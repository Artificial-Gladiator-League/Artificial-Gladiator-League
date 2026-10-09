"""Views for guest accounts."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
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
