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
#  Space URL resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_SPACE_TIMEOUT = 20  # seconds (Gradio SSE stream)
_GRADIO_FN = "get_move"      # Gradio named endpoint exposed by the Space

# Fallback map for known repos whose Space URL can't be derived automatically.
# The Space owner/name on HF need not match the model repo owner/name.
# Store the explicit URL in UserGameModel.hf_inference_endpoint_url to override.
_KNOWN_SPACE_URLS: dict[str, str] = {
    "typical-cyber/chess-model": "https://typical-cyber-typical-cyber.hf.space",
    "test1978/chess-model":      "https://typical-cyber-typical-cyber.hf.space",
}


def _space_url_for(hf_repo_id: str) -> str:
    """Return the Gradio Space *base* URL for *hf_repo_id*.

    Resolution order:
      1. UserGameModel.hf_inference_endpoint_url  (explicit DB override)
      2. _KNOWN_SPACE_URLS  (static fallback map)
      3. CHESS_SPACE_URL Django setting
      4. Last-resort hard-coded default
    """
    try:
        from apps.users.models import UserGameModel
        ugm = UserGameModel.objects.filter(
            hf_model_repo_id=hf_repo_id, game_type="chess",
        ).select_related("user").first()

        if ugm:
            log.info(
                "UGM fields: django_user=%s endpoint_name=%r endpoint_status=%r repo=%s",
                ugm.user.username,
                ugm.hf_inference_endpoint_name,
                ugm.hf_inference_endpoint_status,
                hf_repo_id,
            )
            if ugm.hf_inference_endpoint_url:
                url = ugm.hf_inference_endpoint_url.rstrip("/")
                log.info("Using explicit endpoint URL from DB: %s", url)
                return url

    except Exception:
        log.exception("Failed to look up UGM for repo=%s", hf_repo_id)

    if hf_repo_id in _KNOWN_SPACE_URLS:
        url = _KNOWN_SPACE_URLS[hf_repo_id]
        log.info("Using known Space URL for %s: %s", hf_repo_id, url)
        return url

    try:
        from django.conf import settings
        url = getattr(settings, "CHESS_SPACE_URL", None)
        if url:
            log.warning("Using CHESS_SPACE_URL setting fallback: %s", url)
            return url.rstrip("/")
    except Exception:
        pass

    default = "https://typical-cyber-typical-cyber.hf.space"
    log.warning("Using hard-coded Space URL fallback: %s", default)
    return default


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
    move = _try_space_api(base_url, fen, board, token=hf_token)
    if move:
        latency = _time.monotonic() - _t0
        log.info("[OK] Space move: %s (%.2fs) repo=%s", move, latency, hf_repo_id)
        return move, latency

    # ── Priority 2: random legal move ──
    move = _random_legal_move(board)
    latency = _time.monotonic() - _t0
    log.warning("Random fallback move: %s (%.2fs) repo=%s", move, latency, hf_repo_id)
    return move, latency


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Gradio 4 two-step SSE call
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _try_space_api(
    base_url: str,
    fen: str,
    board: chess.Board,
    *,
    token: str | None = None,
) -> str | None:
    """Call the Gradio 4 Space API and return a validated legal UCI move, or None.

    Gradio 4 uses a two-step pattern:
      1. POST ``/gradio_api/call/{fn}``  → ``{"event_id": "..."}``
      2. GET  ``/gradio_api/call/{fn}/{event_id}``  (SSE) → ``data: ["e2e4"]``

    Payload for step 1: ``{"data": [fen]}``  (Space exposes a single FEN input)
    """
    base = base_url.rstrip("/")
    submit_url = f"{base}/gradio_api/call/{_GRADIO_FN}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        # Step 1 — submit
        resp = requests.post(
            submit_url,
            json={"data": [fen]},
            headers=headers,
            timeout=_SPACE_TIMEOUT,
        )
        resp.raise_for_status()
        event_id = resp.json().get("event_id")
        if not event_id:
            log.warning("Space submit returned no event_id (url=%s)", submit_url)
            return None

        # Step 2 — stream result
        result_url = f"{base}/gradio_api/call/{_GRADIO_FN}/{event_id}"
        log.info("Calling: %s", submit_url)
        stream = requests.get(result_url, stream=True, timeout=_SPACE_TIMEOUT)
        stream.raise_for_status()

        move_str: str | None = None
        complete_seen = False
        error_seen = False
        for raw_line in stream.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if line.startswith("event: error"):
                error_seen = True
            elif line.startswith("event: complete"):
                complete_seen = True
            elif line.startswith("data:"):
                payload = line[len("data:"):].strip()
                try:
                    data = __import__("json").loads(payload)
                    if isinstance(data, list) and data:
                        move_str = str(data[0]).strip()
                except Exception:
                    pass
                # data line follows its event line — stop after reading it
                if complete_seen or error_seen:
                    break

        if not move_str:
            log.warning("Space SSE returned no move for FEN=%s url=%s", fen, base_url)
            return None

        if is_legal_move(board, move_str):
            return move_str

        log.warning(
            "Space returned illegal move %r for FEN=%s url=%s",
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
