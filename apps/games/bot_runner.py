# ──────────────────────────────────────────────
# apps/games/bot_runner.py
#
# Pure game-loop orchestrator for AI-vs-AI games.
# Contains NO model-loading logic — all AI
# inference is delegated to the Docker sandbox
# via the predict_* modules:
#
#   Chess AI logic        → predict_chess.py
#   Breakthrough AI logic → predict_breakthrough.py
#   Chess game rules      → chess_engine.py
#   Breakthrough rules    → breakthrough_engine.py
#
# Models are never loaded in the Django process.
# Each move is executed in an isolated Docker
# container with no network access.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import time

import chess
from django.conf import settings

log = logging.getLogger(__name__)

# Delay between moves so WS spectators can follow along (seconds).
MOVE_DELAY = 0.8

_MOVE_TIMER = time.monotonic  # alias for timing


def _get_repo_for_user(user, game_type: str) -> str | None:
    """Look up the HF repo for a user and game type via UserGameModel.

    Falls back to the legacy ``user.hf_model_repo_id`` for chess.
    """
    if user is None:
        return None
    try:
        repo = user.get_repo_for_game(game_type) or None
    except Exception:
        log.debug("Could not look up repo for %s/%s", user, game_type)
        repo = None

    # If no per-user model is configured for Breakthrough, fall back to
    # a sensible default so bot games don't always hit the random fallback.
    if not repo and game_type == "breakthrough":
        default_repo = getattr(settings, "DEFAULT_BREAKTHROUGH_REPO", "typical-cyber/breakthrough-model")
        log.info("No repo configured for user %s (%s) — falling back to default: %s", getattr(user, 'username', user), game_type, default_repo)
        return default_repo
    return repo


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Move dispatch — routes to the correct predict_*
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_bot_move(
    game_type: str,
    fen: str,
    player: str,
    hf_repo_id: str,
) -> str | None:
    """Ask the appropriate predict module for a move.

    *game_type* — ``'chess'`` or ``'breakthrough'``
    Returns a UCI string, or ``None`` on failure.
    """
    try:
        if game_type == "breakthrough":
            from apps.games.predict_breakthrough import get_move as bt_get_move
            log.info("[bot] Requesting %s move - repo='%s' fen=%.60s",
                     game_type, hf_repo_id, fen)
            _t0 = _MOVE_TIMER()
            move = bt_get_move(fen, player, hf_repo_id)
            _elapsed = _MOVE_TIMER() - _t0
            log.info("[bot] Move received: %s (%.2fs) game_type=%s repo='%s'",
                     move, _elapsed, game_type, hf_repo_id)
            return move
        else:
            from apps.games.predict_chess import get_move as chess_get_move
            log.info("[bot] Requesting %s move - repo='%s' fen=%.60s",
                     game_type, hf_repo_id, fen)
            move, _elapsed = chess_get_move(fen, player, hf_repo_id)
            log.info("[bot] Move received: %s (%.2fs) game_type=%s repo='%s'",
                     move, _elapsed, game_type, hf_repo_id)
            return move
    except Exception as exc:
        # Propagate pre-cache errors so callers fail loudly; otherwise
        # log and return None for unexpected exceptions.
        try:
            from apps.games.exceptions import ModelNotPrecachedError
            if isinstance(exc, ModelNotPrecachedError):
                raise
        except Exception:
            pass
        log.exception(
            "[FAIL] _get_bot_move failed - game_type=%s repo=%s", game_type, hf_repo_id
        )
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bot handle — thin wrapper around repo ID
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class _BotHandle:
    """Lightweight handle that pairs a player's HF repo with a game type."""

    def __init__(self, hf_repo_id: str, hf_token: str | None = None,
                 game_type: str = "chess"):
        self.hf_repo_id = hf_repo_id
        self.hf_token = hf_token
        self.game_type = game_type

    def get_move(self, fen: str, *, time_left=None, opponent_time=None) -> str | None:
        if self.game_type == "breakthrough":
            parts = fen.strip().split()
            player = parts[1] if len(parts) >= 2 else "w"
        else:
            board = chess.Board(fen)
            player = "w" if board.turn == chess.WHITE else "b"
        return _get_bot_move(self.game_type, fen, player, self.hf_repo_id)


def load_bot(hf_repo_id: str, hf_token: str | None = None,
             game_type: str = "chess") -> _BotHandle | None:
    """Return a bot handle for the given HF repo, or None."""
    if not hf_repo_id:
        return None
    return _BotHandle(hf_repo_id, hf_token, game_type)


def get_bot_move(bot, fen: str, time_left: float = None,
                 opponent_time: float = None) -> str | None:
    """Call the bot handle's get_move and return a UCI string, or None."""
    if bot is None:
        return None
    try:
        move = bot.get_move(fen, time_left=time_left, opponent_time=opponent_time)
        return move if isinstance(move, str) else None
    except Exception:
        log.exception("[FAIL] Bot raised an exception during get_move()")
        return None


def preload_models(repo_ids: list[str]) -> None:
    """No-op — models run in Docker sandbox on demand."""
    log.info("preload_models() is a no-op: models run in Docker sandbox.")


def preload_breakthrough_models(repo_ids: list[str]) -> None:
    """No-op — models run in Docker sandbox on demand."""
    log.info("preload_breakthrough_models() is a no-op: models run in Docker sandbox.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Standalone full-game runner (used by tournaments)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_bot_game(game_id: int) -> None:
    """Play an entire game between two AI bots (blocking / synchronous).

    Detects the game type and delegates to the correct runner:
      - Chess        → _run_chess_game()      (rules in chess_engine.py)
      - Breakthrough → _run_breakthrough_game() (rules in breakthrough_engine.py)

    Designed to be called from a background thread so it doesn't block
    the request cycle.  Broadcasts state via the channel layer after
    every move so WebSocket spectators see live updates.
    """
    from apps.games.models import Game

    try:
        game = Game.objects.select_related("white", "black").get(pk=game_id)
    except Game.DoesNotExist:
        log.error("run_bot_game: Game %s not found", game_id)
        return

    if game.is_finished:
        return

    if game.game_type == Game.GameType.BREAKTHROUGH:
        _run_breakthrough_game(game)
    else:
        _run_chess_game(game)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Chess game loop
#  Chess game rules are in chess_engine.py
#  Chess AI logic  is in predict_chess.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _run_chess_game(game) -> None:
    from django.utils import timezone
    from apps.games.models import Game
    from apps.games.chess_engine import (
        make_move, apply_increment, apply_time_spent,
        resolve_armageddon_draw,
    )

    group_name = f"game_{game.pk}"

    white_repo = _get_repo_for_user(game.white, "chess")
    black_repo = _get_repo_for_user(game.black, "chess")

    log.info("[game %s] ===== CHESS GAME START =====", game.pk)
    log.info("[game %s] White: %s (repo=%s)", game.pk,
             game.white.username if game.white else '?', white_repo)
    log.info("[game %s] Black: %s (repo=%s)", game.pk,
             game.black.username if game.black else '?', black_repo)

    # Log cached paths for white/black if present (official handler will use these)
    try:
        from apps.users.models import UserGameModel
        if white_repo:
            w_gm = UserGameModel.objects.filter(hf_model_repo_id=white_repo, game_type='chess').first()
            if w_gm and getattr(w_gm, 'cached_path', None):
                log.info("[game %s] PRE-CACHED white model present at: %s", game.pk, w_gm.cached_path)
        if black_repo:
            b_gm = UserGameModel.objects.filter(hf_model_repo_id=black_repo, game_type='chess').first()
            if b_gm and getattr(b_gm, 'cached_path', None):
                log.info("[game %s] PRE-CACHED black model present at: %s", game.pk, b_gm.cached_path)
    except Exception:
        log.debug("Could not query UserGameModel cached paths", exc_info=True)

    if not white_repo or not black_repo:
        log.error("[FAIL] run_bot_game: Missing HF repo for game %s", game.pk)
        _forfeit_game(game, "white" if not white_repo else "black")
        return

    while not game.is_finished:
        time.sleep(MOVE_DELAY)

        try:
            game.refresh_from_db()
        except Game.DoesNotExist:
            log.info("run_bot_game: Game %s was deleted, stopping.", game.pk)
            return
        if game.is_finished:
            break

        board = game.board()
        moving_color = board.turn

        if moving_color == chess.WHITE:
            repo, bot_time, opp_time = white_repo, game.white_time, game.black_time
            forfeit_color = "white"
            player = "w"
            current_user = game.white
        else:
            repo, bot_time, opp_time = black_repo, game.black_time, game.white_time
            forfeit_color = "black"
            player = "b"
            current_user = game.black

        # ── Anti-cheat SHA check (once per player per game) ──
        # Verify the moving player's HF model SHA still matches the
        # round-pinned baseline. On mismatch the helper prints the
        # 🚨 TERMINAL banner, flips disqualified_for_sha_mismatch
        # (so DisqualificationInterceptMiddleware traps the cheater
        # on /tournaments/disqualified/), emails admins, and broadcasts
        # the WS alert that redirects the cheater's browser. Returns
        # True on FAIL (or already-DQ'd) — forfeit the current move.
        if game.is_tournament_game and current_user is not None:
            try:
                from apps.tournaments.sha_audit import check_player_for_tournament_game
                if check_player_for_tournament_game(game=game, user=current_user):
                    log.warning(
                        "[game %s] Anti-cheat: %s (%s) repo changed mid-game - forfeiting.",
                        game.pk, current_user.username, forfeit_color,
                    )
                    _forfeit_game(game, forfeit_color)
                    _broadcast_game_over(group_name, game)
                    break
            except Exception:
                log.exception(
                    "[game %s] anti-cheat SHA check raised — continuing fail-open",
                    game.pk,
                )

        # Apply elapsed time
        now = timezone.now()
        if game.last_move_at:
            elapsed = (now - game.last_move_at).total_seconds()
            apply_time_spent(game, moving_color, elapsed)
            if game.is_finished:
                game.save()
                _broadcast_game_over(group_name, game)
                break

        # Chess AI logic is in predict_chess.py (runs in Docker sandbox)
        uci = _get_bot_move("chess", game.current_fen, player, repo)
        if not uci:
            log.warning("[FAIL] Bot (%s) failed to produce a move in game %s", forfeit_color, game.pk)
            _forfeit_game(game, forfeit_color)
            _broadcast_game_over(group_name, game)
            break

        ok, err = make_move(game, uci)
        if not ok:
            log.warning("[FAIL] Bot (%s) made illegal move %s in game %s: %s", forfeit_color, uci, game.pk, err)
            _forfeit_game(game, forfeit_color)
            _broadcast_game_over(group_name, game)
            break

        move_count = len(game.move_list) if hasattr(game, 'move_list') else '?'
        log.info("[game %s] Move #%s: %s played %s | white_time=%.1f black_time=%.1f",
                 game.pk, move_count, forfeit_color, uci, game.white_time, game.black_time)

        apply_increment(game, moving_color)
        game.last_move_at = now

        if not game.is_finished:
            game.status = Game.Status.ONGOING

        if game.is_finished:
            if game.armageddon_of is not None and game.result == Game.Result.DRAW:
                resolve_armageddon_draw(game)
            game.save()
            _broadcast_state(group_name, game)
            _broadcast_game_over(group_name, game)
        else:
            game.save()
            _broadcast_state(group_name, game)

    log.info("[game %s] ===== CHESS GAME END - result=%s reason=%s =====",
             game.pk, game.result, getattr(game, 'result_reason', ''))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Breakthrough game loop
#  Breakthrough rules    are in breakthrough_engine.py
#  Breakthrough AI logic is in predict_breakthrough.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _run_breakthrough_game(game) -> None:
    from django.utils import timezone
    from apps.games.models import Game
    from apps.games import breakthrough_engine as bt

    group_name = f"game_{game.pk}"

    white_repo = _get_repo_for_user(game.white, "breakthrough")
    black_repo = _get_repo_for_user(game.black, "breakthrough")

    log.info("[game %s] ===== BREAKTHROUGH GAME START =====", game.pk)
    log.info("[game %s] White: %s (repo=%s)", game.pk,
             game.white.username if game.white else '?', white_repo)
    log.info("[game %s] Black: %s (repo=%s)", game.pk,
             game.black.username if game.black else '?', black_repo)

    # Log cached paths for white/black if present (official handler will use these)
    try:
        from apps.users.models import UserGameModel
        if white_repo:
            w_gm = UserGameModel.objects.filter(hf_model_repo_id=white_repo, game_type='breakthrough').first()
            if w_gm and getattr(w_gm, 'cached_path', None):
                log.info("[game %s] PRE-CACHED white model present at: %s", game.pk, w_gm.cached_path)
        if black_repo:
            b_gm = UserGameModel.objects.filter(hf_model_repo_id=black_repo, game_type='breakthrough').first()
            if b_gm and getattr(b_gm, 'cached_path', None):
                log.info("[game %s] PRE-CACHED black model present at: %s", game.pk, b_gm.cached_path)
    except Exception:
        log.debug("Could not query UserGameModel cached paths", exc_info=True)

    if not white_repo and not black_repo:
        log.error("[FAIL] run_bot_game: Missing HF repos for Breakthrough game %s", game.pk)
        _forfeit_game(game, "white")
        return

    while not game.is_finished:
        time.sleep(MOVE_DELAY)

        try:
            game.refresh_from_db()
        except Game.DoesNotExist:
            log.info("run_bot_game: Breakthrough game %s was deleted, stopping.", game.pk)
            return
        if game.is_finished:
            break

        parts = game.current_fen.strip().split()
        turn = parts[1] if len(parts) >= 2 else bt.WHITE
        forfeit_color = "white" if turn == bt.WHITE else "black"
        repo = white_repo if turn == bt.WHITE else black_repo
        current_user = game.white if turn == bt.WHITE else game.black

        # ── Anti-cheat SHA check (once per player per Breakthrough game) ──
        # Identical pipeline to the chess loop above — see
        # ``check_player_for_tournament_game`` for the full DQ flow.
        if game.is_tournament_game and current_user is not None:
            try:
                from apps.tournaments.sha_audit import check_player_for_tournament_game
                if check_player_for_tournament_game(game=game, user=current_user):
                    log.warning(
                        "[game %s] Anti-cheat: %s (%s) repo changed mid-game - forfeiting Breakthrough.",
                        game.pk, current_user.username, forfeit_color,
                    )
                    _forfeit_game(game, forfeit_color)
                    _broadcast_game_over(group_name, game)
                    break
            except Exception:
                log.exception(
                    "[game %s] anti-cheat SHA check raised in Breakthrough — continuing fail-open",
                    game.pk,
                )

        # Apply elapsed time
        now = timezone.now()
        if game.last_move_at:
            elapsed = (now - game.last_move_at).total_seconds()
            bt.apply_time_spent(game, turn, elapsed)
            if game.is_finished:
                game.save()
                _broadcast_game_over(group_name, game)
                break

        # Breakthrough AI logic is in predict_breakthrough.py (runs in Docker sandbox)
        uci = _get_bot_move("breakthrough", game.current_fen, turn, repo or "")
        if not uci:
            log.warning("[FAIL] Bot (%s) failed to produce a move in Breakthrough game %s", forfeit_color, game.pk)
            _forfeit_game(game, forfeit_color)
            _broadcast_game_over(group_name, game)
            break

        ok, err = bt.make_move(game, uci)
        if not ok:
            log.warning("[FAIL] Bot (%s) made illegal move %s in Breakthrough game %s: %s",
                        forfeit_color, uci, game.pk, err)
            _forfeit_game(game, forfeit_color)
            _broadcast_game_over(group_name, game)
            break

        move_count = len(game.move_list) if hasattr(game, 'move_list') else '?'
        log.info("[game %s] Move #%s: %s played %s | white_time=%.1f black_time=%.1f",
                 game.pk, move_count, forfeit_color, uci, game.white_time, game.black_time)

        bt.apply_increment(game, turn)
        game.last_move_at = now

        if not game.is_finished:
            game.status = Game.Status.ONGOING

        if game.is_finished:
            game.save()
            _broadcast_state(group_name, game)
            _broadcast_game_over(group_name, game)
        else:
            game.save()
            _broadcast_state(group_name, game)

    log.info("[game %s] ===== BREAKTHROUGH GAME END - result=%s reason=%s =====",
             game.pk, game.result, getattr(game, 'result_reason', ''))


def _forfeit_game(game, forfeit_color: str) -> None:
    """Mark a game as forfeited due to bot error."""
    from apps.games.models import Game

    if forfeit_color == "white":
        game.status = Game.Status.BLACK_WINS
        game.result = Game.Result.BLACK_WIN
        game.result_reason = "bot_error"
        game.winner = game.black
    else:
        game.status = Game.Status.WHITE_WINS
        game.result = Game.Result.WHITE_WIN
        game.result_reason = "bot_error"
        game.winner = game.white
    game.save()


def _broadcast_state(group_name: str, game) -> None:
    """Send game state to the channel group (best-effort)."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        last_move = game.move_list[-1] if game.move_list else None
        san_move_list = []
        replay = chess.Board()
        for uci in game.move_list:
            try:
                move_obj = chess.Move.from_uci(uci)
                san_move_list.append(replay.san(move_obj))
                replay.push(move_obj)
            except Exception:
                san_move_list = game.move_list
                break
        payload = {
            "type": "broadcast_state",
            "data": {
                "type": "game_state",
                "game_id": game.pk,
                "fen": game.current_fen,
                "last_move": last_move,
                "move_list": game.move_list,
                "san_move_list": san_move_list,
                "white": game.white.username if game.white else "?",
                "white_elo": game.white.elo if game.white else 0,
                "black": game.black.username if game.black else "?",
                "black_elo": game.black.elo if game.black else 0,
                "white_time": round(game.white_time, 1),
                "black_time": round(game.black_time, 1),
                "increment": game.increment,
                "turn": game.turn_color,
                "status": game.status,
                "result": game.result,
                "time_control": game.time_control,
                "is_armageddon": game.armageddon_of_id is not None,
            },
        }
        async_to_sync(channel_layer.group_send)(group_name, payload)

        # Also broadcast to the tournament match group so spectators
        # watching via LiveMatchConsumer receive live updates.
        if game.is_tournament_game and game.tournament_match_id:
            match_group = f"match_{game.tournament_match_id}"
            is_chess = game.game_type == "chess"
            if last_move and len(last_move) >= 4:
                if is_chess:
                    # Compute SAN and move metadata for the tournament view (chess only)
                    board = chess.Board()
                    for uci in game.move_list[:-1]:
                        board.push_uci(uci)
                    move_obj = chess.Move.from_uci(last_move)
                    san = board.san(move_obj)
                    color = "white" if board.turn == chess.WHITE else "black"
                    board.push(move_obj)
                    is_check = board.is_check()
                else:
                    # Breakthrough: no SAN notation, derive color from FEN turn field
                    parts = game.current_fen.strip().split()
                    # turn field reflects the side that just moved (before the move was applied)
                    color = "white" if (len(parts) < 2 or parts[1] == "b") else "black"
                    san = last_move
                    is_check = False

                async_to_sync(channel_layer.group_send)(match_group, {
                    "type": "match_move",
                    "data": {
                        "type": "move",
                        "fen": game.current_fen,
                        "lastMove": [last_move[:2], last_move[2:4]],
                        "san": san,
                        "color": color,
                        "check": is_check,
                    },
                })
            async_to_sync(channel_layer.group_send)(match_group, {
                "type": "clock_sync",
                "data": {
                    "type": "clock",
                    "white": round(game.white_time, 1),
                    "black": round(game.black_time, 1),
                    "active": game.turn_color,
                },
            })
    except Exception:
        log.debug("Could not broadcast state for game %s", game.pk, exc_info=True)


def _broadcast_game_over(group_name: str, game) -> None:
    """Send game-over event to the channel group (best-effort)."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        payload = {
            "type": "broadcast_game_over",
            "data": {
                "type": "game_over",
                "game_id": game.pk,
                "result": game.result,
                "winner": game.winner.username if game.winner else None,
                "reason": game.result_reason,
                "status": game.status,
            },
        }
        async_to_sync(channel_layer.group_send)(group_name, payload)

        # Notify the lobby that this live game is now finished.
        async_to_sync(channel_layer.group_send)("lobby", {
            "type": "lobby_update",
            "data": {
                "type": "ongoing_game_removed",
                "game_pk": game.pk,
            },
        })

        # Also broadcast to the tournament match group
        if game.is_tournament_game and game.tournament_match_id:
            match_group = f"match_{game.tournament_match_id}"
            async_to_sync(channel_layer.group_send)(match_group, {
                "type": "game_over",
                "data": {
                    "type": "game_over",
                    "result": game.result,
                    "winner": game.winner.username if game.winner else "",
                },
            })
    except Exception:
        log.debug("Could not broadcast game_over for game %s", game.pk)


def _broadcast_armageddon(group_name: str, arm_game) -> None:
    """Send Armageddon announcement to the channel group (best-effort)."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return

        payload = {
            "type": "broadcast_armageddon",
            "data": {
                "type": "armageddon",
                "game_id": arm_game.pk,
                "white": arm_game.white.username if arm_game.white else "?",
                "black": arm_game.black.username if arm_game.black else "?",
                "white_time": arm_game.white_time,
                "black_time": arm_game.black_time,
                "message": "Draw! Armageddon tiebreak starting.",
            },
        }
        async_to_sync(channel_layer.group_send)(group_name, payload)
    except Exception:
        log.debug("Could not broadcast armageddon for game %s", arm_game.pk)
