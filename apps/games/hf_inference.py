# ──────────────────────────────────────────────
# apps/games/hf_inference.py
#
# HF Inference API — calls the Hugging Face Inference
# API (serverless or dedicated endpoint) to get a move.
#
# Architecture
# ────────────
# Each user's model repo contains a handler.py that
# implements the HF custom inference interface.  We call
# it via:
#   1. Dedicated endpoint URL stored on UserGameModel
#      (hf_inference_endpoint_url), if configured.
#   2. HF Serverless Inference API:
#      POST https://api-inference.huggingface.co/models/{repo_id}
#
# Request body:  {"inputs": "<fen_string>"}
# Response body: {"move": "e2e4"} or {"generated_text": "e2e4"}
#                or plain string "e2e4"
#
# Auth: settings.HF_PLATFORM_TOKEN (platform read token).
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import os

import requests
from django.conf import settings

log = logging.getLogger(__name__)

# Timeout for a single HF API call (seconds).
_HF_API_TIMEOUT = int(os.environ.get("HF_API_TIMEOUT", "30"))

_SERVERLESS_BASE = "https://api-inference.huggingface.co/models"


def _platform_token() -> str | None:
    """Return the platform-level HF token, or None."""
    return (
        getattr(settings, "HF_PLATFORM_TOKEN", None)
        or os.environ.get("HF_TOKEN")
    ) or None


def get_move_api(
    repo_id: str,
    fen: str,
    *,
    token: str | None = None,
    endpoint_url: str | None = None,
    timeout: int = _HF_API_TIMEOUT,
) -> str | None:
    """Call the HF Inference API and return a move string, or None on failure.

    Parameters
    ----------
    repo_id:
        HF model repository ID, e.g. ``'test1978/chess-model'``.
    fen:
        Game-state string (FEN for chess, position string for breakthrough).
    token:
        Override HF token.  Defaults to ``HF_PLATFORM_TOKEN``.
    endpoint_url:
        Dedicated endpoint URL.  Defaults to serverless API.
    timeout:
        HTTP timeout in seconds.
    """
    hf_token = token or _platform_token()
    url = endpoint_url or f"{_SERVERLESS_BASE}/{repo_id}"
    headers: dict[str, str] = {}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    headers["Content-Type"] = "application/json"

    try:
        resp = requests.post(
            url,
            json={"inputs": fen},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        log.warning("HF API timeout for repo=%s url=%s", repo_id, url)
        return None
    except requests.exceptions.RequestException as exc:
        log.warning("HF API request failed for repo=%s: %s", repo_id, exc)
        return None

    try:
        data = resp.json()
    except Exception:
        # Plain-text response (some custom handlers return just the move)
        text = resp.text.strip()
        log.debug("HF API plain-text response for repo=%s: %r", repo_id, text)
        return text or None

    # Parse structured response — accept several common layouts
    move = _extract_move(data)
    log.debug("HF API response for repo=%s: %r → move=%r", repo_id, data, move)
    return move


def _extract_move(data) -> str | None:
    """Extract a move string from various HF response shapes."""
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, dict):
        for key in ("move", "generated_text", "output", "result", "prediction"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Some handlers nest under "outputs" or "predictions"
        for key in ("outputs", "predictions"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list) and val:
                return _extract_move(val[0])
    if isinstance(data, list) and data:
        return _extract_move(data[0])
    return None


# ── Compat stubs so old imports don't break ──────────────────────────────────

def verify_model(game_model, *, token: str | None = None, force: bool = False):
    """Stub: SHA-based integrity check via HF API (no local files)."""
    from apps.users.integrity import validate_model_integrity
    from apps.users.models import UserGameModel
    from django.utils import timezone
    hf_token = token or _platform_token() or ""
    try:
        result = validate_model_integrity(game_model, hf_token)
        # validate_model_integrity returns (bool, str)
        ok, msg = result if isinstance(result, tuple) else (bool(result), "")
        new_vs = "approved" if ok else "rejected"
        UserGameModel.objects.filter(pk=game_model.pk).update(
            verification_status=new_vs,
            last_verified_at=timezone.now(),
        )
        game_model.verification_status = new_vs
        return ok, msg, {}
    except Exception as exc:
        return False, str(exc), {}


def reverify_model(game_model, *, token: str | None = None):
    return verify_model(game_model, token=token)


def get_move_local(*args, **kwargs):
    """Removed — use get_move_api()."""
    return None


def download_model(*args, **kwargs):
    """No-op stub."""
    return True, "HF API mode — no local download required", None


def scan_model(*args, **kwargs):
    """No-op stub."""
    return True, {}


# ── Space URL probe ─────────────────────────────────────────────────────────

_PROBE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
_SPACE_PROBE_TIMEOUT = 20


def probe_space_url(base_url: str) -> tuple[bool, str]:
    """Probe a Gradio Space base URL via the /gradio_api/call/get_move endpoint.

    Returns ``(success, message)``.  Does NOT raise — always returns a bool.
    Used by profile view to validate user-supplied Space URLs before saving.

    Only HTTP 2xx responses are treated as success.  4xx responses (including
    404 Not Found) mean the Space does not exist or is not running — these are
    treated as failure so the caller can surface a clear error to the user.
    """
    submit_url = f"{base_url.rstrip('/')}/gradio_api/call/get_move"
    try:
        resp = requests.post(
            submit_url,
            json={"data": [_PROBE_FEN]},
            headers={"Content-Type": "application/json"},
            timeout=_SPACE_PROBE_TIMEOUT,
        )
        if 200 <= resp.status_code < 300:
            return True, f"Space reachable (HTTP {resp.status_code})"
        if resp.status_code == 404:
            return False, "Space not found (HTTP 404) — check the URL or deploy your Space first"
        if resp.status_code == 401 or resp.status_code == 403:
            return False, f"Space access denied (HTTP {resp.status_code}) — Space may be private"
        return False, f"Space returned HTTP {resp.status_code}"
    except requests.exceptions.ConnectionError as exc:
        return False, f"Cannot connect to Space: {exc}"
    except requests.exceptions.Timeout:
        return False, f"Space did not respond within {_SPACE_PROBE_TIMEOUT}s"
    except requests.exceptions.RequestException as exc:
        return False, f"Request failed: {exc}"

