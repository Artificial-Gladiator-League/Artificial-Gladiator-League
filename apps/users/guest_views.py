"""Views for guest accounts."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.users.guest_lifecycle import delete_guest_account
from apps.users.guests import is_guest

log = logging.getLogger(__name__)


@login_required
@require_POST
def guest_delete_data(request):
    """'Delete my guest data now': remove the guest account and every downloaded file."""
    user = request.user
    if not is_guest(user):
        return redirect("users:profile")

    from apps.users.model_lifecycle import _user_ids_in_ongoing_games

    user_id = user.pk
    if user_id in _user_ids_in_ongoing_games():
        messages.error(request, "Finish or resign your current game before deleting your guest data.")
        return redirect("games:lobby")

    logout(request)  # the logout signal already deletes the downloaded snapshots
    if delete_guest_account(user_id):
        messages.success(request, "Your guest data was deleted.")
    else:
        messages.error(request, "We could not delete everything yet. It will be removed automatically.")
    return redirect("core:home")


# ג”€ג”€ Upgrade to a permanent account ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€ג”€
def _send_upgrade_email(req) -> bool:
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.urls import reverse
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    from apps.users.guest_upgrade import UPGRADE_LINK_HOURS
    from apps.users.tokens import guest_upgrade_token

    url = "{}{}".format(
        settings.SITE_URL,
        reverse("users:guest_upgrade_confirm", args=[urlsafe_base64_encode(force_bytes(req.pk)), guest_upgrade_token.make_token(req)]),
    )
    ctx = {"username": req.username, "confirm_url": url, "hours": UPGRADE_LINK_HOURS}
    try:
        msg = EmailMultiAlternatives(
            subject="Confirm your Artificial Gladiator League account",
            body=render_to_string("users/guest_upgrade_email.txt", ctx),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[req.email],
        )
        msg.attach_alternative(render_to_string("users/guest_upgrade_email.html", ctx), "text/html")
        msg.send()
        return True
    except Exception:
        log.exception("Guest upgrade e-mail failed for request %s", req.pk)
        return False


@login_required
def guest_upgrade(request):
    """Registration rules for a guest; the account changes only after the e-mail link is used."""
    from django.contrib.auth.hashers import make_password

    from apps.users.forms import GuestUpgradeForm
    from apps.users.guest_upgrade import create_upgrade_request
    from apps.users.guests import client_ip, rate_allowed

    user = request.user
    if not is_guest(user):
        return redirect("users:profile")

    form = GuestUpgradeForm(request.POST or None, guest=user)
    if request.method == "POST":
        if not rate_allowed("upgrade-user", str(user.pk), settings.GUEST_RATE_UPGRADE_PER_USER) or \
                not rate_allowed("upgrade-ip", client_ip(request), "20/h"):
            form.add_error(None, "Too many attempts. Please try again later.")
        elif form.is_valid():
            req = create_upgrade_request(user, form.cleaned_data, make_password(form.cleaned_data["password"]))
            if _send_upgrade_email(req):
                return redirect("users:guest_upgrade_sent")
            req.delete()
            form.add_error(None, "We couldn't send the confirmation email. Please try again.")
    return render(request, "users/guest_upgrade.html", {
        "form": form, "recaptcha_site_key": settings.RECAPTCHA_PUBLIC_KEY,
    })


def guest_upgrade_sent(request):
    return render(request, "users/guest_upgrade_sent.html")


def guest_upgrade_confirm(request, uidb64, token):
    """Single-use, expiring link from the e-mail. It does not log anyone in."""
    from django.utils import timezone
    from django.utils.http import urlsafe_base64_decode

    from apps.users.guest_upgrade import complete_upgrade
    from apps.users.models import GuestUpgradeRequest
    from apps.users.tokens import guest_upgrade_token

    try:
        req = GuestUpgradeRequest.objects.select_related("user").get(pk=int(urlsafe_base64_decode(uidb64).decode()))
    except (TypeError, ValueError, OverflowError, GuestUpgradeRequest.DoesNotExist):
        req = None
    if req is None or req.used_at or req.expires_at <= timezone.now() or not guest_upgrade_token.check_token(req, token):
        messages.error(request, "This confirmation link is invalid, expired or already used.")
        return redirect("users:login")
    ok, message = complete_upgrade(req.pk)
    (messages.success if ok else messages.error)(request, message)
    return redirect("users:login")
