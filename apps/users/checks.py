from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=False)
def check_guest_mode_settings(app_configs, **kwargs):
    if not getattr(settings, "GUEST_MODE_ENABLED", False):
        return []
    problems = []
    if getattr(settings, "SANDBOX_ENABLE_LOCAL_FALLBACK", False):
        problems.append(Error(
            "GUEST_MODE_ENABLED requires SANDBOX_ENABLE_LOCAL_FALLBACK to be False.",
            hint="Guest model code must only ever run inside the Docker sandbox.",
            id="users.E001",
        ))
    if not getattr(settings, "HF_OAUTH_CLIENT_ID", ""):
        problems.append(Warning(
            "GUEST_MODE_ENABLED is on but HF_OAUTH_CLIENT_ID is empty; guest sign-in cannot work.",
            id="users.W001",
        ))
    if getattr(settings, "GUEST_TTL_DAYS", 0) < 1:
        problems.append(Error("GUEST_TTL_DAYS must be at least 1.", id="users.E002"))
    return problems
