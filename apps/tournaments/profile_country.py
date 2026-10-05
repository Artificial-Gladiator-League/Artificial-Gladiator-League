"""Early country filter based on the user's profile country.

A convenience layer in front of the real checks. It never replaces the IP geo gate,
the per-tournament residency declaration or winner verification, and it only blocks
for the reasons below.

* Country-restricted tournament (``allowed_countries`` is not exactly ["IL"]) and the
  profile has no country  -> send the player to profile settings to set it once.
* Open-to-all tournament (empty ``allowed_countries``) -> never blocks, no country needed.
* Profile country set but not allowed -> blocked with a clear message. This applies to
  money tournaments, where the geo gate also applies. Free Israel-only tournaments
  (QA, ...) have no country gate today and keep having none.
* Israel-only tournaments are NEVER blocked because the country is missing.
"""
from __future__ import annotations

from dataclasses import dataclass

from .countries import ISRAEL_ONLY, countries_phrase

MISSING = "missing"
NOT_ALLOWED = "not_allowed"

# Single source of the text shown to a player whose profile country is not allowed.
NOT_ELIGIBLE_NOTICE = (
    "Your country is not eligible for this tournament, "
    "but you can still take part in other tournaments."
)


@dataclass(frozen=True)
class ProfileCountryVerdict:
    kind: str       # MISSING | NOT_ALLOWED
    message: str


def not_allowed_message(codes) -> str:
    return NOT_ELIGIBLE_NOTICE


def missing_message(codes) -> str:
    return (
        f"This tournament is open to residents of {countries_phrase(codes)} only. "
        "Please set your country of residence in your profile settings first "
        "(it can be set once and is then locked)."
    )


def profile_country_verdict(user, tournament) -> ProfileCountryVerdict | None:
    """None = no objection; otherwise the reason the player must be stopped early."""
    if tournament.is_open_to_all:
        return None
    codes = tournament.allowed_country_codes
    restricted = codes != ISRAEL_ONLY
    country = (getattr(user, "country", "") or "").upper()

    if not country:
        return ProfileCountryVerdict(MISSING, missing_message(codes)) if restricted else None
    if not (restricted or tournament.is_money_tournament):
        return None
    if country not in codes:
        return ProfileCountryVerdict(NOT_ALLOWED, not_allowed_message(codes))
    return None


# ── Staff-visible signal on a participant: profile vs IP vs declared ──────────

AGREE, MISMATCH, NO_DATA = "ok", "mismatch", "n/a"


def country_agreement(participant) -> tuple[str, str]:
    """Compare the three country signals recorded for a participant.

    * profile  - ``profile_country_code`` (profile country at join)
    * ip       - ``join_country_code`` (resolved from the join IP)
    * declared - ``declared_residency_countries`` (codes named in the declaration)

    A disagreement is only a signal for staff; nothing is blocked because of it.
    Returns (state, human readable detail).
    """
    profile = (participant.profile_country_code or "").upper()
    ip = (participant.join_country_code or "").upper()
    declared = [c for c in (participant.declared_residency_countries or "").upper().split(",") if c]

    detail = f"profile={profile or '-'}, ip={ip or '-'}, declared={','.join(declared) or '-'}"
    if sum(bool(x) for x in (profile, ip, declared)) < 2:
        return NO_DATA, detail
    mismatch = bool(profile and ip and profile != ip)
    if declared:
        mismatch = mismatch or bool(profile and profile not in declared) or bool(ip and ip not in declared)
    return (MISMATCH if mismatch else AGREE), detail
