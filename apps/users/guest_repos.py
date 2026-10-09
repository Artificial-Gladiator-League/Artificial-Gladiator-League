"""Gladiator-tab handler for guest accounts: public repos, namespace proof by HF sign-in."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.utils import timezone

from apps.users.guest_snapshot import (
    GuestRepoError,
    check_limits,
    check_not_used_by_another_account,
    fetch_listing,
    schedule_guest_prepare,
    validate_repo_id,
)
from apps.users.guests import rate_allowed
from apps.users.models import UserGameModel

log = logging.getLogger(__name__)

_RESET_ON_REPO_CHANGE = {
    "approved_full_sha": "", "original_model_commit_sha": "", "last_known_commit_id": "",
    "approved_data_repo_sha": "", "current_data_repo_sha": "", "new_data_repo_sha": "",
    "cached_path": "", "cached_commit": "", "cached_at": None,
}


def handle_guest_post(request, game_type: str, repo_id: str, data_repo_id: str, action: str) -> None:
    """Connect (or re-check) a guest's repos. Reports through django messages; never raises."""
    user = request.user
    label = {"chess": "Chess", "breakthrough": "Breakthrough"}.get(game_type, game_type)

    if not rate_allowed("connect", str(user.pk), settings.GUEST_RATE_CONNECT_PER_USER):
        messages.error(request, "Too many repo checks. Please wait a while before trying again.")
        return

    if action == "verify":
        gm = user.get_game_model(game_type)
        if gm is None:
            messages.error(request, "No model registered for this game type yet.")
            return
        gm.status, gm.last_error = UserGameModel.ContractStatus.PENDING, ""
        gm.save(update_fields=["status", "last_error"])
        schedule_guest_prepare(gm.pk, None, None, pin=True)
        messages.info(request, f"Checking your {label} model again. Preparing your model...")
        return

    try:
        validate_repo_id(repo_id, user.guest_hf_username, "model")
        check_not_used_by_another_account(user, "model", repo_id)
        model_listing = fetch_listing(repo_id, "model")
        check_limits(model_listing, repo_id)
        data_listing = None
        if data_repo_id:
            validate_repo_id(data_repo_id, user.guest_hf_username, "data")
            check_not_used_by_another_account(user, "data", data_repo_id)
            data_listing = fetch_listing(data_repo_id, "dataset")
            check_limits(data_listing, data_repo_id)
    except GuestRepoError as exc:
        messages.error(request, str(exc))
        return

    gm = user.get_game_model(game_type) or UserGameModel(user=user, game_type=game_type)
    changed = gm.pk is None or gm.hf_model_repo_id != repo_id or gm.hf_data_repo_id != data_repo_id
    gm.hf_model_repo_id, gm.hf_data_repo_id = repo_id, data_repo_id
    gm.verification_method = UserGameModel.VerificationMethod.OAUTH_NAMESPACE
    gm.is_verified = True
    gm.model_repo_ownership_verified = True
    gm.data_repo_ownership_verified = bool(data_repo_id)
    gm.verification_code = ""
    gm.verified_at = timezone.now()
    gm.status, gm.last_error = UserGameModel.ContractStatus.PENDING, ""
    if changed:
        for field, value in _RESET_ON_REPO_CHANGE.items():
            setattr(gm, field, value)
    gm.save()

    schedule_guest_prepare(gm.pk, model_listing.sha, data_listing.sha if data_listing else None, pin=True)
    messages.info(
        request,
        f"{label} repos saved. Preparing your model... this can take a minute; "
        "the result of the model check appears on this page.",
    )
