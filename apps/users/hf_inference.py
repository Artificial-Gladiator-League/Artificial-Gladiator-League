# ──────────────────────────────────────────────
# apps/users/hf_inference.py
#
# Compatibility shim — the canonical implementation
# now lives in ``apps.games.local_sandbox_inference``.
#
# Re-exports the public API so that existing imports
# in apps/users/ (hf_oauth.py, views.py, etc.)
# continue to work.
# ──────────────────────────────────────────────
from apps.games.local_sandbox_inference import (  # noqa: F401
    verify_model,
    get_move_local,
    reverify_model,
)
