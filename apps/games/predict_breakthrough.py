# ──────────────────────────────────────────────
# apps/games/predict_breakthrough.py
#
# Breakthrough prediction interface for Agladiator.
#
# All AI inference runs inside a Docker sandbox
# on the VPS — no model is loaded in the main
# Django process.
#
# Priority order:
#   1. Call get_move_local (Docker sandbox)
#   2. Random legal move fallback
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import random

from apps.games.breakthrough_engine import (
    legal_moves,
    is_legal_move,
)

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_move(
    fen: str,
    player: str,
    hf_repo_id: str | None = None,
    hf_token: str | None = None,
    *,
    endpoint_url: str | None = None,
) -> str:
    """Return a UCI move string (e.g. 'a2a3') for the given Breakthrough position.

    Parameters
    ----------
    fen : str
        Breakthrough FEN string, e.g.
        ``'BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w'``
    player : str
        ``'w'`` or ``'b'``.
    hf_repo_id : str, optional
        The user's HuggingFace model repository.
    hf_token : str, optional
        Ignored — kept for signature compatibility.
    endpoint_url : str, optional
        Ignored — kept for signature compatibility.

    Returns
    -------
    str
        A legal UCI move.  Never returns an illegal move, never crashes.
    """
    import time as _time

    _t0 = _time.monotonic()
    _short_fen = fen[:60] + ("..." if len(fen) > 60 else "")
    log.info(
        "📡 [BT] get_move called — player=%s repo=%s fen=%s",
        player, hf_repo_id or "(none)", _short_fen,
    )

    all_legal = legal_moves(fen)
    if not all_legal:
        log.error("❌ No legal moves available — position: %s", fen)
        return "0000"

    # ── Priority 1: Docker sandbox inference ──
    if hf_repo_id:
        data_repo_id = _lookup_data_repo_id(hf_repo_id)
        move = _try_sandbox(hf_repo_id, fen, player, all_legal, data_repo_id)
        if move:
            _elapsed = _time.monotonic() - _t0
            log.info(
                "✅ [BT] Priority 1 (sandbox) returned move: %s (%.2fs)",
                move, _elapsed,
            )
            return move

    # ── Priority 2: safe random fallback ──
    move = _random_legal_move(all_legal)
    _elapsed = _time.monotonic() - _t0
    log.warning(
        "⚠️ [BT] Priority 2 (random fallback) returned move: %s (%.2fs)",
        move, _elapsed,
    )
    return move


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Priority 1 — Docker sandbox call
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _try_sandbox(
    hf_repo_id: str,
    fen: str,
    player: str,
    all_legal: list[str],
    data_repo_id: str = "",
) -> str | None:
    """Run inference in the Docker sandbox and return a legal move, or None."""
    try:
        from apps.games.local_sandbox_inference import get_move_local

        move = get_move_local(
            repo_id=hf_repo_id,
            fen=fen,
            player=player,
            game_type="breakthrough",
            data_repo_id=data_repo_id,
        )
        if move and is_legal_move(fen, move):
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
#  Data repo lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _lookup_data_repo_id(hf_repo_id: str) -> str:
    """Look up the data repo ID from UserGameModel by model repo ID."""
    try:
        from apps.users.models import UserGameModel

        gm = UserGameModel.objects.filter(
            hf_model_repo_id=hf_repo_id,
            game_type="breakthrough",
        ).first()
        if gm and gm.hf_data_repo_id:
            return gm.hf_data_repo_id
    except Exception:
        log.debug("Could not look up data repo for %s", hf_repo_id, exc_info=True)
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Random legal move fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _random_legal_move(all_legal: list[str]) -> str:
    """Pick a random legal move, preferring captures."""
    if not all_legal:
        log.error("No legal moves for fallback!")
        return "0000"

    # In Breakthrough, a capture is a diagonal move (files differ)
    captures = [m for m in all_legal if m[0] != m[2]]
    if captures:
        move = random.choice(captures)
        log.info("Fallback chose capture: %s", move)
        return move

    move = random.choice(all_legal)
    log.info("Fallback chose random move: %s", move)
    return move
