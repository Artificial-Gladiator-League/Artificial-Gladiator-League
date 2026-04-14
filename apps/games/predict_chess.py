# ──────────────────────────────────────────────
# apps/games/predict_chess.py
#
# Chess prediction interface for Agladiator.
#
# All AI inference runs inside a Docker sandbox
# on the VPS — no model is loaded in the main
# Django process.
#
# Priority order:
#   1. Call get_move_local (Docker sandbox)
#   2. Random legal move fallback (prefers captures)
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import random

import chess

from apps.games.chess_engine import is_legal_move

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_move(
    fen: str,
    player: str,
    hf_repo_id: str,
    hf_token: str | None = None,
    *,
    endpoint_url: str | None = None,
) -> str:
    """Return a UCI move string (e.g. 'e2e4') for the given position.

    Parameters
    ----------
    fen : str
        Standard chess FEN string.
    player : str
        'w' or 'b'.
    hf_repo_id : str
        The user's HuggingFace model repository.
    hf_token : str, optional
        Ignored — kept for signature compatibility.
    endpoint_url : str, optional
        Ignored — kept for signature compatibility.

    Returns
    -------
    str
        A legal UCI move. Never returns an illegal move, never crashes.
    """
    import time as _time

    _t0 = _time.monotonic()
    board = chess.Board(fen)
    log.info(
        "📡 get_move called — fen=%s player=%s repo=%s",
        fen, player, hf_repo_id,
    )

    # ── Priority 1: Docker sandbox inference ──
    if hf_repo_id:
        move = _try_sandbox(hf_repo_id, fen, player, board)
        if move:
            _elapsed = _time.monotonic() - _t0
            log.info(
                "Priority 1 (sandbox) returned move: %s (%.2fs) repo=%s",
                move, _elapsed, hf_repo_id,
            )
            return move

        # Explicit fallback logging when cache is missing or sandbox fails
        try:
            from apps.users.models import UserGameModel
            gm = UserGameModel.objects.filter(hf_model_repo_id=hf_repo_id, game_type='chess').first()
            if gm is None:
                log.warning("CACHE_MISS: No UserGameModel found for repo=%s — using random fallback.", hf_repo_id)
            elif not getattr(gm, 'cached_path', None):
                log.warning("CACHE_MISS: No cached model for repo=%s — using random fallback.", hf_repo_id)
            else:
                log.warning("SANDBOX_ERROR: Cached model present at %s but sandbox/handler failed — using random fallback.", gm.cached_path)
        except Exception:
            log.debug("Could not query UserGameModel for cache status", exc_info=True)

    # ── Priority 2: safe random fallback ──
    move = _random_legal_move(board)
    _elapsed = _time.monotonic() - _t0
    log.warning(
        "Priority 2 (random fallback) returned move: %s (%.2fs) repo=%s",
        move, _elapsed, hf_repo_id,
    )
    return move


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Priority 1 — Docker sandbox call
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _try_sandbox(
    hf_repo_id: str,
    fen: str,
    player: str,
    board: chess.Board,
) -> str | None:
    """Run inference in the Docker sandbox and return a legal move, or None."""
    try:
        from apps.games.local_sandbox_inference import get_move_local
        # If a cached path exists for this repo, log it — official handler will load from cache
        try:
            from apps.users.models import UserGameModel
            gm = UserGameModel.objects.filter(hf_model_repo_id=hf_repo_id, game_type='chess').first()
            if gm and getattr(gm, 'cached_path', None):
                log.info("✅ Loading model weights from cache: %s", gm.cached_path)
        except Exception:
            pass

        move = get_move_local(
            repo_id=hf_repo_id,
            fen=fen,
            player=player,
            game_type="chess",
        )
        if move and is_legal_move(board, move):
            return move
        if move:
            log.warning(
                "Sandbox returned illegal move '%s' for FEN=%s — falling through.",
                move, fen,
            )
    except Exception:
        log.exception("Sandbox call failed for repo=%s", hf_repo_id)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Random legal move fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _random_legal_move(board: chess.Board) -> str:
    legal = list(board.legal_moves)
    if not legal:
        log.error("No legal moves available — position: %s", board.fen())
        return "0000"

    # Prefer capturing moves
    captures = [m for m in legal if board.is_capture(m)]
    if captures:
        move = random.choice(captures)
        log.info("Fallback chose capture: %s", move.uci())
        return move.uci()

    move = random.choice(legal)
    log.info("Fallback chose random move: %s", move.uci())
    return move.uci()
