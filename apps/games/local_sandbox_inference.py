# ──────────────────────────────────────────────
# apps/games/local_sandbox_inference.py
#
# STUB — Docker sandbox removed.
# Inference now runs via HF Inference API.
# See apps/games/hf_inference.py.
#
# This file is kept only for import compatibility
# with any remaining references.  All functions
# are no-ops or raise DeprecationWarning.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def verify_model(game_model, *, token: str | None = None, force: bool = False):
    """STUB — delegates to HF API integrity check."""
    from apps.games.hf_inference import verify_model as _vm
    return _vm(game_model, token=token, force=force)


def reverify_model(game_model, *, token: str | None = None):
    return verify_model(game_model, token=token)


def get_move_local(*args, **kwargs) -> None:
    """STUB — removed.  Use apps.games.hf_inference.get_move_api()."""
    log.warning("get_move_local() called on stub — sandbox removed")
    return None


def download_model(*args, **kwargs):
    """STUB — no local downloads."""
    return True, "HF API mode — no local download", None


def scan_model(*args, **kwargs):
    """STUB — no local scan."""
    return True, {}


def _get_current_commit_sha(repo_id: str, token: str | None = None) -> str | None:
    """Fetch the latest commit SHA from HF Hub."""
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        info = api.model_info(repo_id, token=token or None)
        return getattr(info, "sha", None)
    except Exception:
        log.debug("Could not fetch SHA for repo=%s", repo_id, exc_info=True)
        return None


def _cleanup_temp_dir(path) -> None:
    """STUB — no temp dirs in HF API mode."""
    pass


def _run_in_local_process(*args, **kwargs):
    """STUB — removed."""
    return None


def _get_move_timeout(*args, **kwargs) -> int:
    from django.conf import settings
    return int(getattr(settings, "SANDBOX_MOVE_TIMEOUT", 30))


def _find_cached_model(*args, **kwargs):
    """STUB — removed."""
    return None

