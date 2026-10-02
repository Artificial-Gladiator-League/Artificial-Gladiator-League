"""Helpers for the per-tournament list of allowed countries (ISO 3166-1 alpha-2)."""
import re

import pycountry

ISRAEL_ONLY = ["IL"]


def default_allowed_countries():
    return list(ISRAEL_ONLY)


def normalize_country_codes(value):
    """Upper-cased, de-duplicated codes from a list or a comma/space separated string."""
    if isinstance(value, str):
        parts = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        return []
    codes = []
    for part in parts:
        code = part.strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes


def unknown_country_codes(codes):
    return [c for c in codes if len(c) != 2 or pycountry.countries.get(alpha_2=c) is None]


def unknown_currency_code(code):
    """True if *code* is not an ISO 4217 alphabetic currency code."""
    return len(code) != 3 or pycountry.currencies.get(alpha_3=code) is None


def country_name(code):
    country = pycountry.countries.get(alpha_2=code) if len(code) == 2 else None
    return getattr(country, "common_name", None) or (country.name if country else code)


def countries_phrase(codes):
    """"India", "India and Israel", "A, B and C"."""
    names = [country_name(c) for c in normalize_country_codes(codes)]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
