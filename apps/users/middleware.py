# ──────────────────────────────────────────────
# apps/users/middleware.py
#
# ModelIntegrityMiddleware
# ────────────────────────
# On the first request of each calendar day,
# redirects authenticated users who have any
# UserGameModel entry to the verify-model page
# where they confirm their HF token for a daily
# model-integrity check.
#
# Exempt paths are listed so the user can still
# log out, access static files, or complete the
# validation itself without triggering a loop.
# ──────────────────────────────────────────────
import logging

from django.shortcuts import redirect
from django.urls import reverse

log = logging.getLogger(__name__)

# URL prefixes that must NOT trigger the redirect.
_EXEMPT_PREFIXES = (
    "/admin/",
    "/static/",
    "/media/",
    "/users/login/",
    "/users/logout/",
    "/users/register/",
    "/users/activate/",
    "/users/activation-sent/",
    "/users/verify-model/",       # the validation page itself
    "/users/password_reset/",
    "/users/reset/",
    "/users/oauth/",              # HF OAuth flow
    "/users/webhooks/",           # HF webhook receiver
)


def _user_has_any_model(user) -> bool:
    """Return True if the user has any UserGameModel with a repo configured."""
    from apps.users.models import UserGameModel
    return UserGameModel.objects.filter(
        user=user, hf_model_repo_id__gt=""
    ).exists() or bool(user.hf_model_repo_id)


class ModelIntegrityMiddleware:
    """Require a daily HF token check before any other page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.user.is_superuser
            and not any(request.path.startswith(p) for p in _EXEMPT_PREFIXES)
            and _user_has_any_model(request.user)
        ):
            from .integrity import needs_daily_validation, validate_all_models

            if needs_daily_validation(request.user):
                log.info(
                    "🔒 Daily validation required for user=%s, redirecting",
                    request.user.username,
                )
                # OAuth users: auto-verify using their stored token
                if request.user.hf_oauth_token_encrypted:
                    from .hf_oauth import get_user_hf_token
                    token = get_user_hf_token(request.user)
                    if token:
                        ok = validate_all_models(request.user, token)
                        if ok:
                            log.info(
                                "✅ Auto-verified via OAuth token for user=%s",
                                request.user.username,
                            )
                            return self.get_response(request)

                        # Validation ran but integrity failed (model changed).
                        # Check whether all models were actually validated today
                        # (i.e. HF was reachable) — if so, flash a warning and
                        # let the user browse; tournaments are auto-blocked by
                        # model_integrity_ok=False.
                        from django.contrib import messages as django_messages
                        from django.utils import timezone as tz
                        from apps.users.models import UserGameModel

                        today = tz.now().date()
                        still_pending = UserGameModel.objects.filter(
                            user=request.user, hf_model_repo_id__gt="",
                        ).exclude(last_model_validation_date=today).exists()

                        if not still_pending:
                            # All models were checked today — integrity issue
                            django_messages.warning(
                                request,
                                "⚠️ A change was detected in your AI model "
                                "repository. Tournament entry is blocked until "
                                "you submit the new revision for approval. "
                                "Casual and rated games are still available.",
                            )
                            log.warning(
                                "⚠️ Model integrity failed for user=%s — "
                                "tournaments blocked, browsing allowed",
                                request.user.username,
                            )
                            return self.get_response(request)

                # Fallback: redirect to manual token entry
                return redirect(reverse("users:verify_model"))

        return self.get_response(request)
