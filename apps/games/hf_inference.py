# ──────────────────────────────────────────────
# apps/games/hf_inference.py
#
# Compatibility shim — HF Inference Endpoints have
# been replaced by the self-hosted Docker sandbox
# system in ``apps.games.local_sandbox_inference``.
#
# This module re-exports the public API so existing
# imports continue to work.  The old endpoint
# functions (create_or_update_endpoint, etc.) are
# replaced by verify_model / get_move_local.
# ──────────────────────────────────────────────
from apps.games.local_sandbox_inference import (  # noqa: F401
    verify_model,
    get_move_local,
    reverify_model,
    download_model,
    scan_model,
)
