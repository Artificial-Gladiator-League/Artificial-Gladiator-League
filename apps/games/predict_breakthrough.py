# ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€
# apps/games/predict_breakthrough.py
#
# Breakthrough prediction ג€” calls the HF Inference API.
#
# Priority order:
#   1. HF Inference API (dedicated endpoint or serverless)
#   2. Random legal move fallback
# ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€
from __future__ import annotations

import logging
import random
import time as _time

from apps.games.breakthrough_engine import (
    legal_moves,
    is_legal_move,
)

log = logging.getLogger(__name__)


# ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”
#  Public API
# ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”
def get_move(
    fen: str,
    player: str,
    hf_repo_id: str | None = None,
    hf_token: str | None = None,
    *,
    endpoint_url: str | None = None,
) -> str:
    """Return a UCI move string for the given Breakthrough position.

    Calls the HF Inference API for ``hf_repo_id``.  Falls back to a
    random legal move if the API is unreachable or returns an invalid move.

    Parameters
    ----------
    fen:
        Breakthrough position string.
    player:
        ``'w'`` or ``'b'``.
    hf_repo_id:
        The user's HuggingFace model repository.
    hf_token:
        Optional override HF token.
    endpoint_url:
        Dedicated endpoint URL (overrides serverless API).
    """
    _t0 = _time.monotonic()

    all_legal = legal_moves(fen)
    if not all_legal:
        log.error("No legal moves available ג€” position: %s", fen)
        return "0000"

    log.info("[BT] get_move: fen=%.60s player=%s repo=%s", fen, player, hf_repo_id)

    # ג”€ג”€ Priority 1: HF Inference API ג”€ג”€
    if hf_repo_id:
        endpoint_url = endpoint_url or _lookup_endpoint_url(hf_repo_id)
        move = _try_hf_api(hf_repo_id, fen, player, all_legal, token=hf_token, endpoint_url=endpoint_url)
        if move:
            log.info(
                "[BT] HF API returned move: %s (%.2fs) repo=%s",
                move, _time.monotonic() - _t0, hf_repo_id,
            )
            return move

    # ג”€ג”€ Priority 2: random legal move ג”€ג”€
    move = _random_legal_move(all_legal)
    log.warning(
        "[BT] Random fallback move: %s (%.2fs) repo=%s",
        move, _time.monotonic() - _t0, hf_repo_id,
    )
    return move


# ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”
#  HF API call
# ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”
def _lookup_endpoint_url(hf_repo_id: str) -> str | None:
    """Return the dedicated endpoint URL stored on UserGameModel, if any."""
    try:
        from apps.users.models import UserGameModel
        gm = UserGameModel.objects.filter(
            hf_model_repo_id=hf_repo_id, game_type="breakthrough",
        ).first()
        if gm:
            return gm.hf_inference_endpoint_url or None
    except Exception:
        pass
    return None


def _try_hf_api(
    hf_repo_id: str,
    fen: str,
    player: str,
    all_legal: list[str],
    *,
    token: str | None = None,
    endpoint_url: str | None = None,
) -> str | None:
    """Call HF Inference API and return a validated legal move, or None."""
    try:
        from apps.games.hf_inference import get_move_api
        raw = get_move_api(
            hf_repo_id, fen,
            token=token, endpoint_url=endpoint_url,
        )
        if not raw:
            return None
        move_str = raw.strip()
        if is_legal_move(fen, move_str):
            return move_str
        log.warning(
            "[BT] HF API returned illegal move %r for fen=%.60s repo=%s",
            move_str, fen, hf_repo_id,
        )
    except Exception:
        log.exception("[BT] HF API call failed for repo=%s", hf_repo_id)
    return None


# ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”
#  Random legal move fallback
# ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”ג”
def _random_legal_move(all_legal: list[str]) -> str:
    """Pick a random legal move, preferring captures (diagonal moves)."""
    if not all_legal:
        return "0000"
    captures = [m for m in all_legal if len(m) >= 4 and m[0] != m[2]]
    if captures:
        return random.choice(captures)
    return random.choice(all_legal)
