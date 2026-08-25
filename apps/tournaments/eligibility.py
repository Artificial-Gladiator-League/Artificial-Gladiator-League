"""
apps/tournaments/eligibility.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
IP-based geolocation eligibility check for money tournaments.

Design
------
* Uses the ipapi.co REST API (no API key required for low-volume use).
* Results are cached in Django's cache backend for GEOIP_CACHE_TIMEOUT seconds
  (default 24 h) to avoid hammering the upstream service.
* Private / loopback IPs are detected without a network call and return
  ``(ip, None, None)`` — "not checked" — so local development always works.
* If the upstream lookup fails the result is ``(ip, None, False)`` — fail
  *closed* — because this is a regulatory gate. The view surfaces a helpful
  error message telling the user to try again or contact support.

Django settings
---------------
    MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES  list[str]  ISO 3166-1 alpha-2 codes
                                                     default: ["IL"]
    MONEY_TOURNAMENT_GEO_ENABLED         bool        default: True
                                                     set False to bypass in dev
    GEOIP_CACHE_TIMEOUT                  int         seconds, default 86400
"""
from __future__ import annotations

import ipaddress
import logging

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────
# Read from settings at *call time* so that @override_settings works in tests.

def _eligible_countries() -> list[str]:
    return getattr(settings, "MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES", ["IL"])

def _geo_enabled() -> bool:
    return getattr(settings, "MONEY_TOURNAMENT_GEO_ENABLED", True)

_API_URL = "https://ipapi.co/{ip}/country/"
_API_TIMEOUT = 5  # seconds


# ── IP helpers ─────────────────────────────────────────────────────

def get_client_ip(request) -> str:
    """Return the most specific public client IP from the request.

    Respects ``X-Forwarded-For`` (set by reverse proxies). The leftmost
    address in the chain is the originating client; all others are proxies.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _is_private_ip(ip: str) -> bool:
    """Return True for loopback, private, or link-local addresses."""
    if not ip:
        return True
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


# ── Geo lookup ─────────────────────────────────────────────────────

def lookup_country(ip: str) -> str | None:
    """Return the ISO 3166-1 alpha-2 country code for *ip*, or ``None`` on error.

    Results are cached for ``GEOIP_CACHE_TIMEOUT`` seconds.
    """
    if not ip:
        return None

    cache_timeout = getattr(settings, "GEOIP_CACHE_TIMEOUT", 86_400)
    cache_key = f"geoip_country_{ip}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # may be a 2-letter code or the sentinel "_FAIL_"

    try:
        resp = requests.get(_API_URL.format(ip=ip), timeout=_API_TIMEOUT)
        resp.raise_for_status()
        code = resp.text.strip().upper()
        if len(code) == 2 and code.isalpha():
            cache.set(cache_key, code, cache_timeout)
            log.debug("geoip: %s → %s", ip, code)
            return code
        log.warning("geoip: unexpected response for %s: %r", ip, resp.text[:80])
    except requests.RequestException as exc:
        log.warning("geoip: lookup failed for %s: %s", ip, exc)

    # Cache the failure sentinel so we don’t hammer the API on every retry
    cache.set(cache_key, "_FAIL_", min(cache_timeout, 300))
    return None


# ── Public API ─────────────────────────────────────────────────────

def check_geo_eligibility(
    request,
) -> tuple[str, str | None, bool | None]:
    """Check whether the request originates from an eligible country.

    Returns
    -------
    (ip, country_code, eligible)

    * ``eligible = True``  — country is in ``ELIGIBLE_COUNTRIES``
    * ``eligible = False`` — country is NOT eligible, or lookup failed
    * ``eligible = None``  — geo check was skipped (private IP, or
      ``MONEY_TOURNAMENT_GEO_ENABLED = False``); treat as allowed in dev
    """
    ip = get_client_ip(request)

    if not _geo_enabled():
        log.debug("geoip: MONEY_TOURNAMENT_GEO_ENABLED=False — skipping check")
        return ip, None, None

    if _is_private_ip(ip):
        log.debug("geoip: private IP %s — skipping check", ip)
        return ip, None, None

    country = lookup_country(ip)
    if country is None or country == "_FAIL_":
        # Lookup failed — fail closed
        log.warning("geoip: closing gate for %s (lookup failed)", ip)
        return ip, None, False

    eligible = country in _eligible_countries()
    log.info("geoip: ip=%s country=%s eligible=%s", ip, country, eligible)
    return ip, country, eligible
