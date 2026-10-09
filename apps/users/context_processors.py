from django.conf import settings


def guest_mode(request):
    """Expose the guest-mode flag so templates can show the guest sign-in blocks."""
    return {"guest_mode_enabled": bool(getattr(settings, "GUEST_MODE_ENABLED", False))}
