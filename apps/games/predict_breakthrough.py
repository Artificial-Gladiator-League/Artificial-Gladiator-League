# ──────────────────────────────────────────────────────────────────────────────
# apps/games/predict_breakthrough.py
#
# Breakthrough prediction — calls the HF Gradio Space API.
#
# Priority order:
#   1. HF Gradio Space (POST /gradio_api/call/get_move → SSE result)
#      Same two-step Gradio 4 pattern used by predict_chess.
#   2. Random legal move fallback
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
import random
import time as _time

import requests

from apps.games.breakthrough_engine import (
    legal_moves,
    is_legal_move,
)

log = logging.getLogger(__name__)

_SPACE_TIMEOUT = 20  # seconds


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
    """Return a UCI move string for the given Breakthrough position.

    Calls the HF Gradio Space for ``hf_repo_id`` via the two-step
    Gradio 4 API (same pattern as predict_chess).  Falls back to a
    random legal move if the Space is unreachable or returns an invalid move.

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
        Override Space base URL (skips repo -> Space derivation).
    """
    _t0 = _time.monotonic()

    all_legal = legal_moves(fen)
    if not all_legal:
        log.error("No legal moves available -- position: %s", fen)
        return "0000"

    log.info("[BT] get_move: fen=%.60s player=%s repo=%s", fen, player, hf_repo_id)

    # -- Priority 1: Gradio Space API --
    if hf_repo_id:
        base_url = endpoint_url or _space_base_url(hf_repo_id)
        move = _try_space_api(base_url, fen, player, all_legal, token=hf_token)
        if move:
            log.info(
                "[BT] Space move: %s (%.2fs) repo=%s",
                move, _time.monotonic() - _t0, hf_repo_id,
            )
            return move

    # -- Priority 2: random legal move --
    move = _random_legal_move(all_legal)
    log.warning(
        "[BT] Random fallback move: %s (%.2fs) repo=%s",
        move, _time.monotonic() - _t0, hf_repo_id,
    )
    return move


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Space URL resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _space_base_url(hf_repo_id: str) -> str:
    """Return the Gradio Space base URL for *hf_repo_id*.

    Resolution order:
      1. UserGameModel.hf_inference_endpoint_url  (explicit DB override)
      2. Derived from repo slug:  owner/name -> https://owner-name.hf.space
    """
    try:
        from apps.users.models import UserGameModel
        gm = UserGameModel.objects.filter(
            hf_model_repo_id=hf_repo_id, game_type="breakthrough",
        ).first()
        if gm and gm.hf_inference_endpoint_url:
            return gm.hf_inference_endpoint_url.rstrip("/")
    except Exception:
        log.exception("[BT] Failed to look up UGM for repo=%s", hf_repo_id)

    # Derive Space URL from repo ID -- mirrors the chess pattern:
    # "owner/space-name" -> "https://owner-space-name.hf.space"
    derived = "https://{}.hf.space".format(hf_repo_id.replace("/", "-"))
    log.info("[BT] Derived Space URL for %s: %s", hf_repo_id, derived)
    return derived


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Gradio RPC call  (POST /run/predict — same pattern as predict_chess)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_SPACE_TIMEOUT = 20  # seconds
_GRADIO_FN = "get_move"   # matches api_name="get_move" in space_app.py


def _try_space_api(
    base_url: str,
    fen: str,
    player: str,
    all_legal: list,
    *,
    token: str | None = None,
) -> str | None:
    """Call the Gradio 4 Space API and return a validated legal move, or None.

    Uses the same two-step SSE pattern as predict_chess._try_space_api:
      1. POST  /gradio_api/call/get_move     -> {"event_id": "..."}
      2. GET   /gradio_api/call/get_move/{id} (SSE) -> data: ["move"]
    """
    base = base_url.rstrip("/")
    submit_url = f"{base}/gradio_api/call/{_GRADIO_FN}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(
            submit_url,
            json={"data": [fen, player]},  # match your Space's inputs
            headers=headers,
            timeout=_SPACE_TIMEOUT,
        )
        resp.raise_for_status()
        event_id = resp.json().get("event_id")
        if not event_id:
            log.warning("[BT] Space submit returned no event_id (url=%s)", submit_url)
            return None

        result_url = f"{base}/gradio_api/call/{_GRADIO_FN}/{event_id}"
        log.info("[BT] Calling: %s", submit_url)
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
                if complete_seen or error_seen:
                    break

        if not move_str:
            log.warning(
                "[BT] Space SSE returned no move for fen=%.60s url=%s",
                fen, base_url,
            )
            return None

        if is_legal_move(fen, move_str):
            return move_str

        log.warning(
            "[BT] Space returned illegal move %r for fen=%.60s url=%s",
            move_str, fen, base_url,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("[BT] Space API call failed (url=%s): %s", base_url, exc)
    except Exception:
        log.exception("[BT] Unexpected error in _try_space_api (url=%s)", base_url)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Random legal move fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _random_legal_move(all_legal: list) -> str:
    """Pick a random legal move, preferring captures (diagonal moves)."""
    if not all_legal:
        return "0000"
    captures = [m for m in all_legal if len(m) >= 4 and m[0] != m[2]]
    if captures:
        return random.choice(captures)
    return random.choice(all_legal)