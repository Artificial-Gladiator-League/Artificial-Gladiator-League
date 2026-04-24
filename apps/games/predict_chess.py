# ──────────────────────────────────────────────
# apps/games/predict_chess.py
#
# Chess prediction — calls the HF Gradio Space API.
#
# Priority order:
#   1. HF Gradio Space (POST /run/predict/ → {"data":["e2e4"]})
#   2. Random legal move fallback (prefers captures)
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import random
import time as _time

import chess
import requests

from apps.games.chess_engine import is_legal_move

log = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Space URL configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Override per-repo or set CHESS_SPACE_URL in Django settings.
_REPO_SPACE_MAP: dict[str, str] = {
    "typical-cyber/chess-model": "https://typical-cyber-typical-cyber.hf.space",
    "test1978/chess-model": "https://typical-cyber-typical-cyber.hf.space",
}
_SPACE_TIMEOUT = 10  # seconds


def _space_url_for(hf_repo_id: str) -> str:
    """Return the Gradio Space base URL for the given repo."""
    if hf_repo_id in _REPO_SPACE_MAP:
        return _REPO_SPACE_MAP[hf_repo_id]
    try:
        from django.conf import settings
        url = getattr(settings, "CHESS_SPACE_URL", None)
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return "https://typical-cyber-typical-cyber.hf.space"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_move(
    fen: str,
    player: str,
    hf_repo_id: str,
    hf_token: str | None = None,
    *,
    space_url: str | None = None,
) -> tuple[str, float]:
    """Return ``(uci_move, latency_seconds)`` for the given position.

    Calls the Gradio Space for ``hf_repo_id`` via
    ``POST <space_url>/run/predict`` with ``{"data": [fen, player, "mcvs"]}``
    and extracts the move from ``response["data"][0]``.
    Falls back to a random legal move if the Space is unreachable or returns
    an invalid move.

    Parameters
    ----------
    fen:
        Standard chess FEN string.
    player:
        ``'w'`` or ``'b'``.
    hf_repo_id:
        The user's HuggingFace model repository (used to resolve Space URL).
    hf_token:
        Optional HF token forwarded as a Bearer header.
    space_url:
        Override the Space base URL (skips repo → Space lookup).
    """
    _t0 = _time.monotonic()

    try:
        board = chess.Board(fen)
    except Exception as exc:
        log.warning("Invalid FEN passed to get_move: %s — %s", fen, exc)
        fallback = _random_legal_move(chess.Board())
        return fallback, _time.monotonic() - _t0

    log.info("get_move: fen=%.60s player=%s repo=%s", fen, player, hf_repo_id)

    # ── Priority 1: Gradio Space API ──
    base_url = space_url or _space_url_for(hf_repo_id)
    move = _try_space_api(base_url, fen, player, board, token=hf_token)
    if move:
        latency = _time.monotonic() - _t0
        log.info("✅ Space move: %s (%.2fs) repo=%s", move, latency, hf_repo_id)
        return move, latency

    # ── Priority 2: random legal move ──
    move = _random_legal_move(board)
    latency = _time.monotonic() - _t0
    log.warning("Random fallback move: %s (%.2fs) repo=%s", move, latency, hf_repo_id)
    return move, latency


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Gradio Space call
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _try_space_api(
    base_url: str,
    fen: str,
    player: str,
    board: chess.Board,
    *,
    token: str | None = None,
) -> str | None:
    """POST ``<base_url>/run/predict`` with Gradio payload and return a validated
    legal move, or None.

    Payload: ``{"data": [fen, player, "mcvs"]}``
    Response: ``{"data": ["<uci_move>"]}``
    """
    try:
        url = base_url.rstrip("/") + "/run/predict"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {"data": [fen, player, "mcvs"]}
        resp = requests.post(url, json=payload, headers=headers, timeout=_SPACE_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            log.warning("Space API returned empty data for FEN=%s url=%s", fen, base_url)
            return None
        move_str = str(data[0]).strip()
        if is_legal_move(board, move_str):
            return move_str
        log.warning(
            "Space API returned illegal move %r for FEN=%s url=%s",
            move_str, fen, base_url,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("Space API call failed (url=%s): %s", base_url, exc)
    except Exception:
        log.exception("Unexpected error in _try_space_api (url=%s)", base_url)
    return None


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
