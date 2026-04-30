"""DisqualificationInterceptMiddleware — traps DQ'd users on the
custom disqualified page until they navigate away to the lobby.

Place this middleware AFTER ``AuthenticationMiddleware`` so
``request.user`` is populated.

It checks every authenticated request for an active
``TournamentParticipant`` row flagged ``disqualified_for_sha_mismatch``
in an ONGOING (or QA-lobby-phase) tournament. If found, the request
is short-circuited with a 302 redirect to ``/tournaments/disqualified/``.

A small whitelist lets through:

* the disqualified page itself (otherwise infinite redirect loop),
* the lobby + home pages (the JS countdown navigates to /games/lobby/),
* the logout endpoint,
* static / media assets,
* the Django admin (so admins can resolve the situation),
* WebSocket/AJAX paths used by the disqualified page itself.

Once the user reaches the lobby they are no longer interacting with
the tournament UI, satisfying the "trap from the tournament" UX.
The flag itself stays True until an admin clears it (or until the
tournament leaves ONGOING status), so any attempt to navigate back
into a tournament URL re-triggers the interception.
"""
from __future__ import annotations

import logging

from django.shortcuts import redirect
from django.urls import reverse, NoReverseMatch

log = logging.getLogger(__name__)


# Path PREFIXES that are always allowed even when the user is DQ'd.
# Keep this list short — anything reachable from here MUST NOT expose
# tournament gameplay surfaces.
_ALLOWED_PREFIXES = (
    "/static/",
    "/media/",
    "/admin/",
    "/accounts/logout/",
    "/users/logout/",
    "/logout/",
    "/games/lobby/",          # Destination of the JS countdown redirect.
    "/",                       # Home page (handled below as exact match).
    "/favicon.ico",
)

# Exact path matches that are always allowed.
_ALLOWED_EXACT = {
    "/",
    "/favicon.ico",
}


class DisqualificationInterceptMiddleware:
    """Force-redirect DQ'd users to the disqualified landing page."""

    def __init__(self, get_response):
        self.get_response = get_response
        try:
            self._dq_url = reverse("tournaments:disqualified")
        except NoReverseMatch:
            # URL not wired yet (e.g. during initial migration).
            self._dq_url = "/tournaments/disqualified/"

    def __call__(self, request):
        # Anonymous users + non-GET-friendly paths skip the check
        # entirely so login / signup flows are never blocked.
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return self.get_response(request)

        path = request.path or "/"

        # Always allow the DQ page itself — otherwise we infinite-loop.
        if path == self._dq_url or path.startswith(self._dq_url):
            return self.get_response(request)

        # Static-ish allowlist.
        if path in _ALLOWED_EXACT:
            return self.get_response(request)
        if any(
            path.startswith(prefix)
            for prefix in _ALLOWED_PREFIXES
            if prefix != "/"
        ):
            return self.get_response(request)

        # The actual DB check.  Imported lazily so this module stays
        # importable during migrations / system checks.
        try:
            from apps.tournaments.disqualification import find_active_dq_participant
        except Exception:
            return self.get_response(request)

        try:
            participant = find_active_dq_participant(user)
        except Exception:
            log.debug(
                "DisqualificationInterceptMiddleware: lookup failed",
                exc_info=True,
            )
            return self.get_response(request)

        if participant is None:
            return self.get_response(request)

        # Stash the offending participant on the session so the
        # disqualified view can render context-rich messaging (which
        # tournament, which round, which repo) without a second query.
        try:
            request.session["dq_participant_id"] = participant.pk
            request.session["dq_tournament_id"] = participant.tournament_id
            request.session["dq_tournament_name"] = participant.tournament.name
        except Exception:
            pass

        log.warning(
            "DisqualificationInterceptMiddleware: redirecting user=%s "
            "from %s to %s (participant=%s tournament=%s)",
            user.username, path, self._dq_url,
            participant.pk, participant.tournament_id,
        )
        return redirect(self._dq_url)
