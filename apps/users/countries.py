"""Country-of-residence helpers (ISO 3166-1 alpha-2), backed by pycountry.

This is a convenience layer for the profile and for an early tournament filter.
It never replaces the IP geo gate, the per-tournament residency declaration or
winner verification.
"""
from functools import lru_cache

import pycountry


def _display_name(country) -> str:
    return getattr(country, "common_name", None) or country.name


@lru_cache(maxsize=1)
def _names() -> dict:
    return {c.alpha_2: _display_name(c) for c in pycountry.countries}


def is_valid_country(code) -> bool:
    return isinstance(code, str) and code.upper() in _names()


def country_name(code) -> str:
    """Display name for *code*; the code itself when unknown, empty string when blank."""
    if not code:
        return ""
    return _names().get(code.upper(), code)


@lru_cache(maxsize=1)
def country_choices() -> tuple:
    """(code, name) pairs sorted by name."""
    return tuple(sorted(_names().items(), key=lambda kv: kv[1].casefold()))


def flag_static_path(code) -> str:
    """Path of the flag image inside the static tree (shipped by django-countries)."""
    return f"flags/{code.lower()}.gif"
