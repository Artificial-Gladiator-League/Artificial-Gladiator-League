"""Guest -> permanent account: the same user row is updated once the e-mail link is confirmed."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.users.guests import RESERVED_USERNAME_PREFIX
from apps.users.models import GuestUpgradeRequest

log = logging.getLogger(__name__)

UPGRADE_LINK_HOURS = 24


def create_upgrade_request(user, cleaned: dict, password_hash: str) -> GuestUpgradeRequest:
    """Store the pending upgrade (replacing older ones). The password is kept hashed."""
    import secrets

    now = timezone.now()
    GuestUpgradeRequest.objects.filter(user=user, used_at__isnull=True).update(expires_at=now)
    return GuestUpgradeRequest.objects.create(
        user=user, nonce=secrets.token_urlsafe(32),
        username=cleaned["username"], ai_name=cleaned["ai_name"], email=cleaned["email"],
        country=cleaned["country"], password_hash=password_hash, consented_at=now,
        expires_at=now + timedelta(hours=UPGRADE_LINK_HOURS),
    )


def complete_upgrade(request_id: int) -> tuple[bool, str]:
    """Turn the guest row into a permanent account in one transaction. Returns (ok, message)."""
    User = get_user_model()
    try:
        with transaction.atomic():
            req = GuestUpgradeRequest.objects.select_for_update().get(pk=request_id)
            user = User.objects.select_for_update().get(pk=req.user_id)
            now = timezone.now()
            if req.used_at is not None:
                return False, "This link has already been used."
            if req.expires_at <= now:
                return False, "This link has expired. Please start the upgrade again."
            if not user.is_guest:
                return False, "This account is already permanent."

            others = User.objects.exclude(pk=user.pk)
            if req.username.lower().startswith(RESERVED_USERNAME_PREFIX) or others.filter(username__iexact=req.username).exists():
                return False, "That username is no longer available. Please start the upgrade again."
            if others.filter(email__iexact=req.email).exists():
                return False, "That email address is already in use. Please start the upgrade again."
            if others.filter(ai_name__iexact=req.ai_name).exists():
                return False, "That AI name is no longer available. Please start the upgrade again."
            if not user.claim_country(req.country) and user.country != req.country:
                return False, "A different country is already saved for this account. Please contact support."

            User.objects.filter(pk=user.pk).update(
                username=req.username, email=req.email, ai_name=req.ai_name, password=req.password_hash,
                is_guest=False, is_active=True,
                guest_created_at=None, guest_last_seen=None, guest_expires_at=None,
            )
            req.used_at = now
            req.save(update_fields=["used_at"])
            GuestUpgradeRequest.objects.filter(user_id=user.pk, used_at__isnull=True).update(expires_at=now)
    except GuestUpgradeRequest.DoesNotExist:
        return False, "This link is invalid."
    except IntegrityError:
        log.warning("Guest upgrade %s hit a uniqueness conflict", request_id)
        return False, "That username or AI name is no longer available. Please start the upgrade again."
    log.info("Guest %s upgraded to a permanent account", req.user_id)
    return True, "Your account is now permanent. Log in with your new username and password."
