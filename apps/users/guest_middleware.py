"""Deny-by-default access control for guest accounts (HTTP)."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.core.cache import cache
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils import timezone

from apps.users.guests import guest_blocked_response, guest_mode_enabled, is_guest

log = logging.getLogger(__name__)

GET = frozenset({"GET", "HEAD", "OPTIONS"})
ANY = None  # every method; the view itself decides what a guest may do

# Guests may reach only these views. Everything else (including every view added
# later) is refused. Read-only pages are GET-only so no write path is exposed.
GUEST_ALLOWED_VIEWS: dict[str, frozenset | None] = {
    # public information
    "core:home": GET, "core:privacy": GET, "core:about": GET, "core:terms": GET,
    "core:accessibility": GET, "core:cookies": GET, "core:how_to_upload": GET,
    "core:how_it_works": GET, "core:contact": GET, "core:presence": GET,
    # read-only leaderboard / news / forum / tournaments
    "core:leaderboard": GET, "core:leaderboard_json": GET,
    "news:list": GET, "news:detail": GET,
    "forum:category_list": GET, "forum:topic_list": GET, "forum:topic_detail": GET,
    "tournaments:list": GET, "tournaments:detail": GET, "tournaments:fair_play": GET,
    "tournaments:how_it_works": GET, "tournaments:gauntlet": GET,
    "tournaments:gauntlet_by_pk": GET, "tournaments:gauntlet_standings_partial": GET,
    # own account: Gladiator tab, diagnostics, upgrade, deletion
    "users:logout": ANY, "users:profile": ANY, "users:save_gladiator_field": ANY,
    "users:model_file_status_api": GET, "users:run_diagnostics": ANY,
    "users:diagnostics_status": GET, "users:diagnostics_latest": GET,
    "users:activity_heatmap": GET, "users:user_search": GET, "users:public_profile": GET,
    "users:match_moves": GET, "users:game_moves": GET,
    "users:guest_upgrade": ANY, "users:guest_upgrade_sent": GET,
    "users:guest_upgrade_confirm": GET, "users:guest_delete_data": ANY,
    # guest games (isolation is enforced inside the views)
    "games:lobby": GET, "games:create_game": ANY, "games:create_lobby_game": ANY,
    "games:join_game": ANY, "games:cancel_game": ANY, "games:leave_game": ANY,
    "games:next_live_ai_game": GET, "games:spectate": GET, "games:game_detail": GET,
    "games:history": GET, "games:game_comments": GET,
}


def _is_static_path(path: str) -> bool:
    for prefix in (getattr(settings, "STATIC_URL", "/static/"), getattr(settings, "MEDIA_URL", "/media/")):
        if prefix and path.startswith(prefix):
            return True
    return path.endswith("favicon.ico")


class GuestAccessMiddleware:
    """For guest users only: end the session when guest mode is off, refresh activity,
    refuse everything outside GUEST_ALLOWED_VIEWS and mark all responses noindex."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if not is_guest(user):
            return self.get_response(request)

        if not guest_mode_enabled():
            logout(request)
            return redirect("/")

        self._touch(user)

        if not self._allowed(request):
            response = guest_blocked_response(request)
        else:
            response = self.get_response(request)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @staticmethod
    def _allowed(request) -> bool:
        path = request.path_info
        if _is_static_path(path):
            return True
        try:
            match = resolve(path)
        except Resolver404:
            return True  # a 404 leaks nothing
        methods = GUEST_ALLOWED_VIEWS.get(match.view_name, False)
        if methods is False:
            return False
        return methods is None or request.method in methods

    @staticmethod
    def _touch(user) -> None:
        """Slide the guest's expiry; at most one write a minute per guest."""
        key = f"guest-seen:{user.pk}"
        if cache.get(key):
            return
        cache.set(key, 1, timeout=60)
        now = timezone.now()
        get_user_model().objects.filter(pk=user.pk, is_guest=True).update(
            guest_last_seen=now,
            guest_expires_at=now + timedelta(days=settings.GUEST_TTL_DAYS),
        )
