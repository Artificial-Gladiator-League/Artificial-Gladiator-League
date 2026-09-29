# ──────────────────────────────────────────────
# apps/games/hf_inference.py
#
# Compatibility shim.  All live inference now runs via the Docker
# sandbox (apps.games.local_inference / apps.games.sandbox_runner) —
# there is no HF Inference API / HF Space call path anymore.
#
# The functions below are intentionally no-ops, kept only so old
# imports (apps.users.hf_inference re-exports these names) don't break.
# ──────────────────────────────────────────────
from __future__ import annotations


def verify_model(game_model, *, token: str | None = None, force: bool = False):
    """Stub: integrity checks only run at tournament registration."""
    return True, "OK", {}


def reverify_model(game_model, *, token: str | None = None):
    return verify_model(game_model, token=token)


def get_move_local(*args, **kwargs):
    """Stub: use apps.games.local_inference.get_move_local() (Docker sandbox)."""
    return None


def download_model(*args, **kwargs):
    """No-op stub."""
    return True, "Docker sandbox mode — no runtime download required", None


def scan_model(*args, **kwargs):
    """No-op stub."""
    return True, {}


