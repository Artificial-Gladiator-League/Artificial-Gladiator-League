# ──────────────────────────────────────────────
# apps/users/middleware.py
#
# ModelIntegrityMiddleware — no-op passthrough.
#
# Integrity checks now run ONLY at tournament
# registration time (tournaments/views.py →
# join_tournament).  This middleware no longer
# intercepts requests.
# ──────────────────────────────────────────────


class ModelIntegrityMiddleware:
    """No-op passthrough — integrity checks run only at tournament registration."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)
