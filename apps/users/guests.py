"""Shared helpers for guest mode (inert unless guest accounts exist / GUEST_MODE_ENABLED is on)."""
from __future__ import annotations

import ipaddress
import logging
import time
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

log = logging.getLogger(__name__)

GUEST_BLOCK_MESSAGE = "Create a permanent account to compete"


def guest_mode_enabled() -> bool:
    return bool(getattr(settings, "GUEST_MODE_ENABLED", False))


def is_guest(user) -> bool:
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_guest", False))


def client_ip(request) -> str:
    """Client address: REMOTE_ADDR, or the last value of TRUSTED_PROXY_HEADER when that is set.

    The last value is the one appended by our own proxy, so it cannot be forged by the client.
    """
    header = getattr(settings, "TRUSTED_PROXY_HEADER", "") or ""
    if header:
        raw = request.META.get("HTTP_" + header.upper().replace("-", "_"), "")
        candidate = raw.split(",")[-1].strip() if raw else ""
        if candidate:
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                log.warning("Ignoring malformed %s header value", header)
    return request.META.get("REMOTE_ADDR", "")


_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def rate_allowed(scope: str, ident: str, rate: str) -> bool:
    """Fixed-window counter in the Django cache. Returns False once *rate* ("N/h") is exceeded."""
    count, _, unit = rate.partition("/")
    limit, seconds = int(count), _PERIODS[unit[:1].lower() or "h"]
    key = f"guest-rl:{scope}:{ident}:{int(time.time() // seconds)}"
    cache.add(key, 0, timeout=seconds + 5)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=seconds + 5)
        used = 1
    return used <= limit


def _wants_json(request) -> bool:
    accept = request.headers.get("Accept", "")
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in accept
        or request.content_type == "application/json"
    )


def guest_blocked_response(request, status: int = 403):
    if _wants_json(request):
        return JsonResponse({"error": GUEST_BLOCK_MESSAGE, "guest_blocked": True}, status=status)
    return render(request, "users/guest_blocked.html", {"message": GUEST_BLOCK_MESSAGE}, status=status)


def registered_required(view):
    """Server-side block for guests on top of login_required (defence in depth)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if is_guest(request.user):
            return guest_blocked_response(request)
        return view(request, *args, **kwargs)
    return wrapper
