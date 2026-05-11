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
import os
import shutil
from apps.games.model_preloader import preload_user_models, clear_user_models
from django.conf import settings
from pathlib import Path
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.games.chess_engine import compute_elo_deltas
from apps.games.models import Game
from apps.users.models import CustomUser, UserGameModel

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

    # Compute ELO deltas using the per-game ELO fields so Chess and
    # Breakthrough ratings never bleed into each other.
    game_type = getattr(instance, "game_type", "chess") or "chess"
    white_game_elo = white.get_elo_for_game(game_type)
    black_game_elo = black.get_elo_for_game(game_type)

    delta_w, delta_b = compute_elo_deltas(
        winner=instance.winner, loser=None,
        white=white, black=black,
        result=instance.result,
        white_elo=white_game_elo,
        black_elo=black_game_elo,
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

    # Apply per-game ELO deltas; set_elo_for_game also keeps the legacy
    # user.elo as max(elo_chess, elo_breakthrough) for backward compat.
    white.set_elo_for_game(game_type, white_game_elo + delta_w)
    black.set_elo_for_game(game_type, black_game_elo + delta_b)

    white.save(update_fields=[
        "elo", "elo_chess", "elo_breakthrough",
        "wins", "losses", "draws", "total_games", "current_streak",
    ])
    black.save(update_fields=[
        "elo", "elo_chess", "elo_breakthrough",
        "wins", "losses", "draws", "total_games", "current_streak",
    ])

    # ── Rated-game tracking & model locking ───────
    # Tournament games DO NOT count toward the 30-game qualification
    # threshold — only casual (non-tournament) games do.
    from apps.users.rating_lock import record_rated_game
    from apps.users.models import UserGameModel
    from django.db.models import F

    is_tournament_game = bool(instance.is_tournament_game)

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

    if not is_tournament_game:
        # Only non-tournament games count toward the qualification gate.
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


# ── Auto-pin SHA for legacy models on first finished game ──────

def _auto_pin_legacy_sha(player, game_type: str) -> None:
    """Resolve and persist ``approved_full_sha`` for a legacy UGM row.

    Called after a finished Game when the player's UserGameModel for
    *game_type* still has an empty ``approved_full_sha``. Mirrors the
    write set performed by the ``pin_chess_model_shas`` management
    command so manual backfill and signal-driven backfill stay in sync.

    Errors are logged and swallowed — this must never crash the post_save
    chain for a Game.
    """
    try:
        from apps.users.integrity import _get_stored_token, _resolve_ref_sha
        from apps.users.models import UserGameModel
    except Exception:
        log.exception("auto_pin: import failed")
        return

    try:
        gm = UserGameModel.objects.filter(
            user=player, game_type=game_type,
        ).first()
    except Exception:
        log.exception("auto_pin: DB lookup failed for user=%s game=%s",
                      getattr(player, "username", "?"), game_type)
        return

    if gm is None:
        return
    if (gm.approved_full_sha or "").strip():
        return  # already pinned
    repo_id = (gm.hf_model_repo_id or "").strip()
    if not repo_id:
        return

    token = _get_stored_token(player) or ""
    if not token:
        log.info(
            "auto_pin: skipped user=%s repo=%s — no HF token available",
            getattr(player, "username", "?"), repo_id,
        )
        return

    ref = (gm.submitted_ref or "main").strip() or "main"
    repo_type = (gm.submission_repo_type or "model").strip() or "model"
    try:
        latest_sha = _resolve_ref_sha(repo_id, token, ref=ref, repo_type=repo_type)
    except Exception:
        log.exception("auto_pin: _resolve_ref_sha raised for repo=%s", repo_id)
        return
    if not latest_sha:
        log.info("auto_pin: HF returned no sha for repo=%s ref=%s", repo_id, ref)
        return

    now = timezone.now()
    gm.approved_full_sha = latest_sha
    gm.original_model_commit_sha = gm.original_model_commit_sha or latest_sha
    gm.last_known_commit_id = latest_sha
    gm.model_integrity_ok = True
    gm.last_model_validation_date = now.date()
    if not gm.pinned_at:
        gm.pinned_at = now
    try:
        gm.save(update_fields=[
            "approved_full_sha",
            "original_model_commit_sha",
            "last_known_commit_id",
            "model_integrity_ok",
            "last_model_validation_date",
            "pinned_at",
        ])
        log.info(
            "auto_pin: pinned legacy SHA user=%s game=%s repo=%s sha=%s",
            getattr(player, "username", "?"), game_type, repo_id, latest_sha[:12],
        )
    except Exception:
        log.exception("auto_pin: save failed for user=%s repo=%s",
                      getattr(player, "username", "?"), repo_id)


@receiver(post_save, sender=Game)
def auto_pin_sha_after_first_game(sender, instance, **kwargs):
    """Backfill ``approved_full_sha`` the first time a finished Game is saved.

    Targets legacy users whose UserGameModel still has an empty SHA. We
    only run on terminal games and only for players whose SHA is missing,
    so the HF lookup happens at most once per (user, game_type).
    """
    if instance.result in ("*", ""):
        return
    if instance.white is None or instance.black is None:
        return
    game_type = getattr(instance, "game_type", "chess") or "chess"
    for player in (instance.white, instance.black):
        try:
            _auto_pin_legacy_sha(player, game_type)
        except Exception:
            log.exception(
                "auto_pin_sha_after_first_game: unexpected error for user=%s",
                getattr(player, "username", "?"),
            )


# ── Login-time model verification ──────────────

def _verify_local_models_for_user(user) -> None:
    """Verify committed local model files are present for all of the user's game models.

    Replaces the old HF-download-based _preload_for_user().
    No network I/O — reads only from the git-committed repo paths.

    For each UserGameModel:
      1. Call verify_local_files() — checks files exist and are non-empty.
      2. Set local_path on the record if files are present.
      3. Optionally run the full sandbox verification (scan + smoke test)
         in a nested thread (non-blocking, best-effort).
    """
    try:
        from django.conf import settings
        from apps.games.local_inference import (
            verify_local_files,
            _game_type_dir,
            verify_model as _verify_model,
        )
        from apps.users.models import UserGameModel

        # Local-inference is opt-in. When it isn't required globally we
        # still set local_path on success (cheap, harmless) but we do
        # NOT warn / notify on missing files — those users run via the
        # remote HF inference path and missing local files are expected.
        local_required = bool(getattr(settings, "LOCAL_MODEL_REQUIRED", False))

        game_models = UserGameModel.objects.filter(user=user)
        if not game_models.exists():
            log.info("[local] %s — no game models registered, skipping local file check", user.username)
            return

        for gm in game_models:
            game_type = gm.game_type
            ok, msg = verify_local_files(gm)
            if not ok:
                if local_required:
                    log.warning(
                        "[local] %s/%s — local files missing: %s",
                        user.username, game_type, msg,
                    )
                    print(f"⚠️ [local] {user.username}/{game_type} — local files missing: {msg}")
                    _notify_user_verification_failed(user, game_type, msg)
                else:
                    # Expected when local inference isn't enabled —
                    # downgrade to info and skip the user notification.
                    log.info(
                        "[local] %s/%s — local files not present (local inference not required): %s",
                        user.username, game_type, msg,
                    )
                continue

            # Files exist — set local_path so inference can find them fast.
            try:
                gt_dir = _game_type_dir(gm.user_id, game_type)
                if gm.local_path != str(gt_dir):
                    gm.local_path = str(gt_dir)
                    gm.save(update_fields=["local_path"])
                    log.info(
                        "[local] %s/%s — local_path set: %s",
                        user.username, game_type, gt_dir,
                    )
            except Exception:
                log.exception("[local] Failed to update local_path for %s/%s", user.username, game_type)

            print(f"✅ [local] {user.username}/{game_type} — local model files confirmed at {gm.local_path}")

            # Background verification (non-blocking, best-effort).
            def _sandbox_verify(_gm=gm, _user=user):
                try:
                    passed, vmsg, _ = _verify_model(_gm)
                    if passed:
                        print(
                            f"[local] {_user.username}/{_gm.game_type} "
                            f"— verification passed"
                        )
                    else:
                        print(
                            f"[local] {_user.username}/{_gm.game_type} "
                            f"— verification failed: {vmsg}"
                        )
                        _notify_user_verification_failed(_user, _gm.game_type, vmsg)
                except Exception:
                    log.exception(
                        "[local] Verify failed for %s/%s",
                        _user.username, _gm.game_type,
                    )

            t = threading.Thread(
                target=_sandbox_verify,
                daemon=True,
                name=f"verify-{user.pk}-{game_type}",
            )
            t.start()

    except Exception:
        log.exception("[local] _verify_local_models_for_user failed for %s", getattr(user, "username", "?"))


def _notify_user_verification_failed(user, game_type: str, error_msg: str):
    """Send a WebSocket notification when model verification fails."""
    try:
        from channels.layers import get_channel_layer
        from apps.core.consumers import notif_group_name

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
    """Verify local model files for the user after login.

    Runs in a background thread so login is never delayed or blocked.
    Steps performed (all non-fatal):
      1. Ensure per-user directory skeleton exists.
      2. Verify committed local model files are present (no HF downloads).
      3. Set local_path on each UserGameModel so inference can resolve
         model directories without hitting the database repeatedly.

    Note: HF snapshot_download() is NOT called here. All model files
    must be pre-committed to user_models/user_{id}/{game}/model/.
    """
    if user is None:
        return

    user_pk = getattr(user, "pk", None)
    if user_pk is None:
        return

    def _background():
        try:
            # Ensure per-user dirs exist (creates placeholders if absent).
            try:
                from apps.users.model_lifecycle import ensure_user_dirs
                ensure_user_dirs(user_pk)
            except Exception:
                log.exception("Failed to ensure user dirs for user=%s on login", user_pk)

            # Verify local committed model files and update local_path.
            _verify_local_models_for_user(user)

        except Exception:
            log.exception(
                "Background login task failed for user %s", getattr(user, "username", "?"),
            )

    t = threading.Thread(target=_background, daemon=True, name=f"local-login-{user_pk}")
    t.start()


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
        user_id = getattr(user, "pk", None)
        if user_id is None:
            return

        try:
            clear_user_models(user_id)
            log.info("Removed preloaded models for user %s on logout", user.username)
        except Exception:
            log.exception("Failed to remove preloaded models for user %s on logout", user.username)
    except Exception:
        log.exception("Failed to start logout cleanup for user %s", getattr(user, "username", "?"))


# ── Ensure per-user dirs created on user creation and on new UserGameModel ──
@receiver(post_save, sender=CustomUser)
def create_user_dirs_on_create(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from apps.users.model_lifecycle import ensure_user_dirs
        ensure_user_dirs(instance.pk)
        log.info("Ensured model dirs for new user %s", instance.pk)
    except Exception:
        log.exception("Failed to create user dirs for %s", instance.pk)


@receiver(post_save, sender=UserGameModel)
def ensure_game_dirs_on_gamemodel_create(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from apps.users.model_lifecycle import ensure_user_dirs
        ensure_user_dirs(instance.user_id)
        log.info("Ensured model dirs for user %s game %s", instance.user_id, instance.game_type)
    except Exception:
        log.exception("Failed to ensure game dirs for user %s game %s", instance.user_id, instance.game_type)


# ── Auto-populate HF Space URL ─────────────────────────────────────────────

def _derive_space_url(repo_id: str) -> str:
    """Derive the HF Gradio Space base URL from a model repo ID.

    HF Space URL convention: spaces named ``owner/owner`` produce
    ``https://owner-owner.hf.space``. This matches the pattern used
    by verify_chess_models._space_base_url() as its generic fallback.
    """
    owner = repo_id.split("/")[0] if "/" in repo_id else repo_id
    return f"https://{owner}-{owner}.hf.space"


@receiver(post_save, sender=UserGameModel)
def populate_hf_space_url(sender, instance, **kwargs):
    """Auto-populate hf_inference_endpoint_url when a model repo is registered.

    Runs synchronously on save (no network I/O — pure string derivation).
    A background thread then probes the derived URL and updates
    hf_inference_endpoint_status to 'ready' or 'failed'.

    Uses .update() instead of .save() to avoid re-triggering this signal.
    """
    if not instance.hf_model_repo_id:
        return
    if instance.hf_inference_endpoint_url:
        return  # already set — respect explicit overrides

    derived_url = _derive_space_url(instance.hf_model_repo_id)
    UserGameModel.objects.filter(pk=instance.pk).update(
        hf_inference_endpoint_url=derived_url,
        hf_inference_endpoint_status="pending",
    )
    log.info(
        "Auto-derived Space URL for UGM %s (user=%s repo=%s): %s",
        instance.pk,
        getattr(instance, "user_id", "?"),
        instance.hf_model_repo_id,
        derived_url,
    )

    # Kick off a non-blocking probe in a background thread so we can
    # immediately mark the endpoint 'ready' or 'failed' without
    # blocking the save path or requiring Celery.
    def _probe(ugm_pk: int, url: str, game_type: str) -> None:
        import requests as _req
        try:
            resp = _req.post(
                f"{url}/gradio_api/call/get_move",
                json={"data": ["rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"]},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            new_status = "ready" if resp.status_code < 500 else "failed"
        except Exception as exc:
            log.warning("Space probe failed for UGM %s url=%s: %s", ugm_pk, url, exc)
            new_status = "failed"

        UserGameModel.objects.filter(pk=ugm_pk).update(
            hf_inference_endpoint_status=new_status,
        )
        log.info("Space probe result for UGM %s: status=%s", ugm_pk, new_status)

    t = threading.Thread(
        target=_probe,
        args=(instance.pk, derived_url, instance.game_type),
        daemon=True,
        name=f"space-probe-{instance.pk}",
    )
    t.start()
