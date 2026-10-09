"""Guest sign-in with Hugging Face (OIDC, scopes "openid profile" only).

Only the HF username and the stable ``sub`` are read from the userinfo response.
The access token never leaves ``hf_oauth_callback``: it is not passed to this module,
stored, or logged.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.users.guests import client_ip, guest_mode_enabled, rate_allowed

log = logging.getLogger(__name__)

REGISTERED_MESSAGE = (
    "This Hugging Face account belongs to a registered user. "
    "Please log in with your username and password."
)
_HF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _random(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def active_guest_count() -> int:
    return get_user_model().objects.filter(is_guest=True).count()


@require_POST
def guest_hf_start(request):
    """Begin the guest sign-in: CSRF-protected POST with the consent checkbox ticked."""
    if not guest_mode_enabled():
        raise Http404
    if request.user.is_authenticated:
        return redirect("games:lobby")
    if not request.POST.get("guest_consent"):
        messages.error(request, "Please tick the box to accept the Terms and Privacy Policy before continuing as a guest.")
        return redirect("users:login")
    if not settings.HF_OAUTH_CLIENT_ID:
        messages.error(request, "Hugging Face sign-in is not configured.")
        return redirect("users:login")
    if not rate_allowed("signin-ip", client_ip(request), settings.GUEST_RATE_SIGNIN_PER_IP):
        messages.error(request, "Too many guest sign-in attempts. Please try again later.")
        return redirect("users:login")
    if active_guest_count() >= settings.GUEST_MAX_ACTIVE:
        messages.error(request, "Guest play is full right now. Please try again later.")
        return redirect("users:login")

    # Reuse the existing PKCE helper; the guest flow is bound to its own state value.
    from apps.users.hf_oauth import _generate_pkce

    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)
    request.session["hf_oauth_state"] = state
    request.session["hf_oauth_verifier"] = verifier
    request.session["hf_oauth_guest_state"] = state

    params = {
        "client_id": settings.HF_OAUTH_CLIENT_ID,
        "redirect_uri": request.build_absolute_uri(reverse("users:hf_oauth_callback")),
        "response_type": "code",
        "scope": settings.GUEST_OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = requests.Request("GET", settings.HF_OAUTH_AUTHORIZE_URL, params=params).prepare().url
    return redirect(url)


def _belongs_to_registered_user(hf_username: str) -> bool:
    """True if a non-guest account is linked to, or has a verified repo under, this HF name."""
    from apps.users.models import CustomUser, UserGameModel

    if CustomUser.objects.filter(is_guest=False, hf_username__iexact=hf_username).exists():
        return True
    prefix = f"{hf_username}/"
    verified = UserGameModel.objects.filter(user__is_guest=False, is_verified=True)
    return (
        verified.filter(hf_model_repo_id__istartswith=prefix).exists()
        or verified.filter(hf_data_repo_id__istartswith=prefix).exists()
    )


def _unique_username() -> str:
    User = get_user_model()
    while True:
        name = "guest_" + _random(8)
        if not User.objects.filter(username__iexact=name).exists():
            return name


def _unique_ai_name(hf_username: str) -> str:
    User = get_user_model()
    name = hf_username
    while User.objects.filter(ai_name__iexact=name).exists():
        name = f"{hf_username}-{_random(4)}"
    return name


def _create_guest(sub: str, hf_username: str):
    User = get_user_model()
    now = timezone.now()
    user = User(
        username=_unique_username(), email="", is_active=True, is_guest=True,
        ai_name=_unique_ai_name(hf_username), hf_sub=sub, guest_hf_username=hf_username,
        guest_created_at=now, guest_last_seen=now,
        guest_expires_at=now + timedelta(days=settings.GUEST_TTL_DAYS),
    )
    user.set_unusable_password()
    user.save()
    return user


def complete_guest_sign_in(request, userinfo: dict):
    """Callback branch for the guest flow. *userinfo* holds the HF profile (no token)."""
    User = get_user_model()
    if not guest_mode_enabled():
        raise Http404
    if request.user.is_authenticated:
        return redirect("games:lobby")

    sub = str(userinfo.get("sub") or "").strip()
    hf_username = str(userinfo.get("preferred_username") or userinfo.get("name") or "").strip()
    if not sub or len(sub) > 128 or not _HF_NAME_RE.match(hf_username):
        messages.error(request, "Could not read your Hugging Face profile. Please try again.")
        return redirect("users:login")
    if not rate_allowed("signin-sub", sub, settings.GUEST_RATE_SIGNIN_PER_SUB):
        messages.error(request, "Too many sign-in attempts for this Hugging Face account. Please try again later.")
        return redirect("users:login")

    existing = User.objects.filter(hf_sub=sub).first()
    if existing is not None:
        if not existing.is_guest:
            messages.error(request, REGISTERED_MESSAGE)
            return redirect("users:login")
        return _log_guest_in(request, existing, welcome_back=True)

    if _belongs_to_registered_user(hf_username):
        messages.error(request, REGISTERED_MESSAGE)
        return redirect("users:login")
    if active_guest_count() >= settings.GUEST_MAX_ACTIVE:
        messages.error(request, "Guest play is full right now. Please try again later.")
        return redirect("users:login")

    try:
        with transaction.atomic():
            user = _create_guest(sub, hf_username)
    except IntegrityError:
        user = User.objects.filter(hf_sub=sub, is_guest=True).first()
        if user is None:
            messages.error(request, "Could not create your guest account. Please try again.")
            return redirect("users:login")
        return _log_guest_in(request, user, welcome_back=True)
    log.info("Guest account created: user=%s", user.pk)
    return _log_guest_in(request, user, welcome_back=False)


def _log_guest_in(request, user, *, welcome_back: bool):
    now = timezone.now()
    User = get_user_model()
    User.objects.filter(pk=user.pk).update(
        guest_last_seen=now, guest_expires_at=now + timedelta(days=settings.GUEST_TTL_DAYS),
    )
    login(request, user)
    from apps.users.guest_snapshot import prepare_pinned_snapshots
    try:
        prepare_pinned_snapshots(user)
    except Exception:
        log.exception("Could not queue snapshot preparation for guest %s", user.pk)
    messages.success(
        request,
        "Welcome back. Your guest account is active again." if welcome_back
        else "Welcome! Connect your public Hugging Face repos to start playing.",
    )
    return redirect(reverse("users:profile") + "?tab=ai-models")
