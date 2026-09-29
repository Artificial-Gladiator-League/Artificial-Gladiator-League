# ──────────────────────────────────────────────
# apps/games/predict_chess.py
#
# Chess prediction — runs the user's model in the Docker sandbox.
#
# Priority order:
#   1. Docker sandbox (apps.games.local_inference.get_move_local())
#   2. Random legal move fallback (prefers captures) — UNLESS the model's
#      last contract check failed (UserGameModel.status == FAILED), in
#      which case ``None`` is returned so the caller (apps.games.bot_runner)
#      forfeits the game instead of silently playing random moves on the
#      user's behalf.
#
# SandboxUnavailableError (Docker daemon/image unreachable) is an
# infrastructure fault and is NOT caught here — it propagates to the
# caller (apps.games.bot_runner) instead of silently falling back to
# a random move.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import random
import time as _time

import chess

from apps.games.chess_engine import is_legal_move

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Repo → UserGameModel resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _game_model_for_repo(hf_repo_id: str):
    """Look up the owning UserGameModel for a chess repo, or None if unregistered."""
    try:
        from apps.users.models import UserGameModel
        return UserGameModel.objects.filter(
            hf_model_repo_id=hf_repo_id, game_type="chess",
        ).first()
    except Exception:
        log.exception("Failed to look up UGM for repo=%s", hf_repo_id)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_move(
    fen: str,
    player: str,
    hf_repo_id: str,
    hf_token: str | None = None,
) -> tuple[str | None, float]:
    """Return ``(uci_move, latency_seconds)`` for the given position.

    Runs the user's model in the Docker sandbox via
    ``apps.games.local_inference.get_move_local()``. Falls back to a
    random legal move if the sandbox ran but returned no valid move —
    unless the model's last contract check failed, in which case
    ``uci_move`` is ``None`` so the caller forfeits instead of playing on
    the user's behalf with random moves.

    Parameters
    ----------
    fen:
        Standard chess FEN string.
    player:
        ``'w'`` or ``'b'``.
    hf_repo_id:
        The user's registered model repository (used to resolve user_id).
    hf_token:
        Unused (kept for call-site compatibility).
    """
    _t0 = _time.monotonic()

    try:
        board = chess.Board(fen)
    except Exception as exc:
        log.warning("Invalid FEN passed to get_move: %s — %s", fen, exc)
        fallback = _random_legal_move(chess.Board())
        return fallback, _time.monotonic() - _t0

    log.info("get_move: fen=%.60s player=%s repo=%s", fen, player, hf_repo_id)

    # ── Priority 1: Docker sandbox ──
    from apps.games.local_inference import get_move_local

    game_model = _game_model_for_repo(hf_repo_id)
    user_id = game_model.user_id if game_model is not None else None
    if user_id is not None:
        move = get_move_local(user_id, "chess", fen, player, repo_id=hf_repo_id)
        if move and is_legal_move(board, move):
            latency = _time.monotonic() - _t0
            log.info("[OK] Sandbox move: %s (%.2fs) repo=%s", move, latency, hf_repo_id)
            return move, latency
        if move:
            log.warning(
                "Sandbox returned illegal move %r for FEN=%s repo=%s",
                move, fen, hf_repo_id,
            )
    else:
        log.warning("No UserGameModel registered for repo=%s (chess)", hf_repo_id)

    # A model whose contract check failed must not be quietly replaced by
    # random moves — return None so the caller forfeits the game.
    if game_model is not None and game_model.status == game_model.ContractStatus.FAILED:
        latency = _time.monotonic() - _t0
        log.warning(
            "Model status=FAILED for repo=%s — refusing random fallback, forfeiting", hf_repo_id,
        )
        return None, latency

    # ── Priority 2: random legal move ──
    move = _random_legal_move(board)
    latency = _time.monotonic() - _t0
    log.warning("Random fallback move: %s (%.2fs) repo=%s", move, latency, hf_repo_id)
    return move, latency


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Random legal move fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _random_legal_move(board: chess.Board) -> str:
    legal = list(board.legal_moves)
    if not legal:
        log.error("No legal moves available — position: %s", board.fen())
        return "0000"
    captures = [m for m in legal if board.is_capture(m)]
    if captures:
        return random.choice(captures).uci()
    return random.choice(legal).uci()

