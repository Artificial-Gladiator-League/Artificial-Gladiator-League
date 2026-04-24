# ──────────────────────────────────────────────
# apps/users/hf_inference.py
#
# Shim — re-exports from apps.games.hf_inference
# so that existing imports in apps/users/ continue
# to work without modification.
# ──────────────────────────────────────────────
from apps.games.hf_inference import (  # noqa: F401
    verify_model,
    reverify_model,
    get_move_local,
    download_model,
    scan_model,
)

