# ──────────────────────────────────────────────
# apps/users/signals.py
# Post‑save handler for Game that updates
# CustomUser aggregated stats and ELO, then
# pushes a real‑time leaderboard refresh.
#
# NOTE: Only the Game signal updates stats.
# The Match signal was removed to prevent
# double-counting — every tournament Match
# result always originates from a Game save,
# so the Game handler is the single source of
# truth for stat/ELO updates.
# ──────────────────────────────────────────────
import logging
import sys
import threading

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.games.chess_engine import compute_elo_deltas
from apps.games.models import Game

log = logging.getLogger(__name__)


# ── Stat updater ───────────────────────────────

def _update_player_stats(player, is_winner, is_draw):
    """Increment aggregated counters and streak on a CustomUser instance."""
    player.total_games += 1
    if is_draw:
        player.draws += 1
        player.current_streak = 0
    elif is_winner:
        player.wins += 1
        player.current_streak = max(player.current_streak, 0) + 1
    else:
        player.losses += 1
        player.current_streak = min(player.current_streak, 0) - 1


# ── Leaderboard broadcast ─────────────────────

def _broadcast_leaderboard_refresh(p1, p2):
    """Push a lightweight update to all leaderboard WS clients."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        payload = {
            "type": "leaderboard_update",
            "data": {
                "type": "refresh",
                "players": [
                    {
                        "username": p1.username,
                        "elo": p1.elo,
                        "wins": p1.wins,
                        "losses": p1.losses,
                        "draws": p1.draws,
                        "streak": p1.current_streak,
                    },
                    {
                        "username": p2.username,
                        "elo": p2.elo,
                        "wins": p2.wins,
                        "losses": p2.losses,
                        "draws": p2.draws,
                        "streak": p2.current_streak,
                    },
                ],
            },
        }
        async_to_sync(channel_layer.group_send)("leaderboard", payload)
    except Exception:
        log.exception("Failed to broadcast leaderboard update")


# ── Game signal (casual + tournament games) ────

@receiver(post_save, sender=Game)
def update_stats_after_game(sender, instance, **kwargs):
    """Recalculate ELO and stats whenever a Game is saved with a final result.

    This is the **only** signal that touches player stats.  Tournament
    bracket advancement is delegated to ``handle_game_result()`` which
    updates the Match row but does NOT re-count stats (the old Match
    signal that did so has been removed to prevent double-counting).
    """
    if instance.result in ("*", ""):
        return  # game not finished yet
    if instance.white is None or instance.black is None:
        return  # no opponent paired yet

    # Guard: check the DB row to see if ELO deltas were already recorded.
    # This prevents double-counting if the game is saved again after finishing.
    db_vals = Game.objects.filter(pk=instance.pk).values_list(
        "elo_change_white", "elo_change_black",
    ).first()
    if db_vals and (db_vals[0] != 0 or db_vals[1] != 0):
        return

    white = instance.white
    black = instance.black

    # Compute ELO deltas (uses provisional K-factor for new players)
    delta_w, delta_b = compute_elo_deltas(
        winner=instance.winner, loser=None,
        white=white, black=black,
        result=instance.result,
    )

    # Persist deltas on the game row (use update to avoid re‑triggering)
    Game.objects.filter(pk=instance.pk).update(
        elo_change_white=delta_w, elo_change_black=delta_b,
    )

    # Determine outcome per player
    is_draw = instance.result == "1/2-1/2"

    # Guard: if the game has a decisive result but no winner set,
    # refuse to update stats — this prevents double-loss corruption.
    if not is_draw and instance.winner is None:
        log.error(
            "Game %s has result=%r but winner is None — skipping stat update. "
            "Fix the game-ending code to always set game.winner.",
            instance.pk, instance.result,
        )
        return

    w_won = (instance.winner_id == white.pk) if not is_draw else False
    b_won = (instance.winner_id == black.pk) if not is_draw else False

    _update_player_stats(white, w_won, is_draw)
    _update_player_stats(black, b_won, is_draw)

    white.elo += delta_w
    black.elo += delta_b

    white.save(update_fields=["elo", "wins", "losses", "draws", "total_games", "current_streak"])
    black.save(update_fields=["elo", "wins", "losses", "draws", "total_games", "current_streak"])

    # ── Rated-game tracking & model locking ───────
    # Every finished game counts as a rated game.  Once a user reaches
    # MANDATORY_RATED_GAMES (30), their model commit SHA is frozen.
    from apps.users.rating_lock import record_rated_game
    from apps.users.models import UserGameModel
    from django.db.models import F

    game_type = getattr(instance, "game_type", "chess") or "chess"

    # Auto-create UserGameModel for legacy users who only have
    # hf_model_repo_id on CustomUser but no per-game entry yet.
    # Without this, their rated_games_since_revalidation never
    # increments and they can never meet the 30-game tournament gate.
    for player in (white, black):
        if not UserGameModel.objects.filter(user=player, game_type=game_type).exists():
            repo = player.hf_model_repo_id if game_type == "chess" else ""
            if repo:
                UserGameModel.objects.create(
                    user=player,
                    game_type=game_type,
                    hf_model_repo_id=repo,
                    model_integrity_ok=player.model_integrity_ok,
                    original_model_commit_sha=player.original_model_commit_sha or "",
                    last_known_commit_id=player.last_known_commit_id or "",
                    rated_games_played=player.rated_games_played,
                )
                log.info(
                    "Auto-created UserGameModel for legacy user %s/%s (repo=%s)",
                    player.username, game_type, repo,
                )

    record_rated_game(white, game_type=game_type)
    record_rated_game(black, game_type=game_type)

    # Increment per-game revalidation counter for both players
    UserGameModel.objects.filter(
        user__in=[white, black], game_type=game_type,
    ).update(rated_games_since_revalidation=F("rated_games_since_revalidation") + 1)

    _broadcast_leaderboard_refresh(white, black)

    # If this is a tournament game, trigger bracket advancement
    if instance.is_tournament_game and instance.tournament_match:
        from apps.tournaments.engine import handle_game_result
        handle_game_result(instance)


# ── Login-time model verification ──────────────

def _preload_for_user(user):
    """Background verification of all user models (Chess + Breakthrough) at login.

    Uses the Docker sandbox pipeline from local_sandbox_inference:
      Phase 1 — Download to /tmp/verification/{user_id}_{game_type}_{ts}/
      Phase 2 — Security scan (bandit + modelscan + fickling + picklescan)
      Phase 3 — Test in Docker (--network=none, --read-only)
    Zero persistent storage: all temp files are deleted after every run.
    """
    try:
        from apps.games.local_sandbox_inference import verify_model
        from apps.users.models import UserGameModel
        from apps.users.hf_oauth import get_user_hf_token

        hf_token = get_user_hf_token(user)

        # Verify all game models the user has registered
        game_models = UserGameModel.objects.filter(
            user=user, hf_model_repo_id__gt="",
        )
        if not game_models.exists():
            print(f"[ONLINE] {user.username} — no models registered, skipping verification")
            return

        for gm in game_models:
            game_type = gm.game_type
            repo_id = gm.hf_model_repo_id

            print(f"🔄 [ONLINE] {user.username} — Starting {game_type} model verification...")
            print(f"📥 Downloading model repo {repo_id} ...")

            try:
                passed, msg, report = verify_model(gm, token=hf_token)

                if passed:
                    print(f"✅ [ONLINE] {user.username}/{game_type} — Verification complete — model approved and ready to play")
                else:
                    print(f"❌ [ONLINE] {user.username}/{game_type} — Verification FAILED: {msg}")
                    _notify_user_verification_failed(user, game_type, msg)
            except Exception as e:
                print(f"❌ [ONLINE] {user.username}/{game_type} — Verification error: {e}")
                _notify_user_verification_failed(user, game_type, str(e))

            print(f"🗑️ Cleaning up temporary files (zero persistent storage)")

    except Exception as e:
        print(f"[ONLINE] {user.username} — ❌ verification setup failed: {e}")


def _notify_user_verification_failed(user, game_type: str, error_msg: str):
    """Send a WebSocket notification when model verification fails."""
    try:
        from channels.layers import get_channel_layer
        from apps.chat.consumers import notif_group_name

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            notif_group_name(user.pk),
            {
                "type": "send_notification",
                "verb": f"{game_type}_verification_failed",
                "actor": "",
                "message": (
                    f"⚠️ Your {game_type.title()} model failed verification: {error_msg}. "
                    f"Please check your model repo and ensure it uses SafeTensors format."
                ),
                "url": "",
                "unread_count": 0,
            },
        )
    except Exception:
        pass  # WebSocket broadcast is best-effort


@receiver(user_logged_in)
def preload_breakthrough_on_login(sender, request, user, **kwargs):
    """Download & verify models for the user after login.

    Always runs ``download_and_scan_for_user`` directly in a background
    thread so it works regardless of whether a Celery worker is running.
    """
    if user is None:
        return
    try:
        from apps.users.model_lifecycle import download_and_scan_for_user

        user_pk = getattr(user, "pk", None)
        if user_pk is None:
            return

        def _bg():
            try:
                download_and_scan_for_user(user_pk)
            except Exception:
                log.exception("Background login scan failed for user=%s", user_pk)

        threading.Thread(target=_bg, daemon=True).start()
        log.info("Started background download_and_scan for user %s", user.username)
    except Exception:
        log.exception("Failed to start download task for user %s", getattr(user, "username", "?"))


# ── Logout handler (no-op — zero persistent storage) ──

@receiver(user_logged_out)
def clear_breakthrough_on_logout(sender, request, user, **kwargs):
    """Clean up cached model files on logout.

    Always runs ``cleanup_for_user`` directly in a background thread so
    it works regardless of whether a Celery worker is running.
    """
    if user is None:
        return
    try:
        from apps.users.model_lifecycle import cleanup_for_user

        user_id = getattr(user, "pk", None)
        if user_id is None:
            return

        def _bg():
            try:
                cleanup_for_user(user_id)
            except Exception:
                log.exception("Background logout cleanup failed for user=%s", user_id)

        threading.Thread(target=_bg, daemon=True).start()
        log.info("Started background cleanup for user %s", user.username)
    except Exception:
        log.exception("Failed to start logout cleanup for user %s", getattr(user, "username", "?"))
