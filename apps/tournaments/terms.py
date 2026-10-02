"""Rendering of TournamentTerms bodies.

Bodies are admin-authored HTML containing Django template syntax. They are
rendered with a dedicated engine that has no loaders or extra tag libraries,
so a body can only use core tags/filters and the context it is given.
"""
import logging

from django.template import Context, Engine
from django.utils.safestring import mark_safe

from .countries import countries_phrase

log = logging.getLogger(__name__)

_ENGINE = Engine(autoescape=True, libraries={}, loaders=[])

# Shown when a tournament has no TournamentTerms record (pre-terms behaviour).
LEGACY_TERMS_TITLE = "Money Tournament"
LEGACY_TERMS_VERSION = "1.0"


def compile_body(body):
    """Return a compiled template for *body*; raises TemplateSyntaxError."""
    return _ENGINE.from_string(body)


def render_body(terms, tournament):
    """Render ``terms.body`` with only ``tournament`` and ``terms`` in context."""
    return compile_body(terms.body).render(
        Context({"tournament": tournament, "terms": terms})
    )


def build_terms_context(tournament):
    """Template context for the terms page.

    A tournament without a terms record keeps the legacy behaviour: the built-in
    Gauntlet text, every box shown and all checkboxes required.
    """
    terms = tournament.terms
    required = required_confirmations(tournament)
    declaration = {
        "israel_only": tournament.is_israel_only,
        "residency_field_name": residency_field_name(tournament),
        "residency_countries": countries_phrase(tournament.allowed_country_codes),
        "payout_method_label": tournament.get_payout_method_display(),
        "payout_instructions": tournament.payout_instructions,
        "show_payout_box": (terms is None or terms.has_prize) and (
            bool(tournament.payout_instructions) or not tournament.uses_paypal
        ),
    }
    if terms is None:
        return {
            "terms": None,
            "terms_title": LEGACY_TERMS_TITLE,
            "terms_version": LEGACY_TERMS_VERSION,
            "terms_body": "",
            "show_prize_box": True,
            "show_paypal_box": paypal_email_required(tournament),
            "show_geo_notice": True,
            "require_age": required["age"],
            "require_residency": required["residency"],
            **declaration,
        }
    return {
        "terms": terms,
        "terms_title": terms.title,
        "terms_version": terms.version,
        # Only the rendered result is marked safe, never the raw body.
        "terms_body": mark_safe(render_body(terms, tournament)),
        "show_prize_box": terms.has_prize,
        "show_paypal_box": paypal_email_required(tournament),
        "show_geo_notice": terms.requires_israeli_residency,
        "require_age": required["age"],
        "require_residency": required["residency"],
        **declaration,
    }


def residency_field_name(tournament):
    """POST field carrying the residency declaration; Israel-only keeps its original name."""
    return "confirmed_israeli_resident" if tournament.is_israel_only else "confirmed_residency"


def required_confirmations(tournament):
    """Which confirmations the join POST must carry for *tournament*.

    Without a terms record: age and residency always, the terms box only when the
    legacy ``terms_version`` is set (unchanged behaviour).
    """
    terms = tournament.terms
    if terms is None:
        return {"age": True, "residency": True, "terms": bool(tournament.terms_version)}
    return {
        "age": terms.requires_age_18,
        "residency": terms.requires_israeli_residency,
        "terms": True,
    }


def paypal_email_required(tournament):
    """A PayPal address is needed only for PayPal payouts, and only when the terms carry a prize."""
    if not tournament.uses_paypal:
        return False
    terms = tournament.terms
    return True if terms is None else terms.has_prize


def paypal_email_ok(user, tournament):
    """The single PayPal rule shared by the terms page and the join view."""
    return (not paypal_email_required(tournament)) or bool(user.paypal_email)


def posted_terms_match(tournament, post):
    """True if the slug/version the form carried are the tournament's current terms."""
    terms = tournament.terms
    expected = (terms.slug, terms.version) if terms else ("", "")
    return (post.get("terms_slug", ""), post.get("terms_version", "")) == expected


def accepted_terms_fields(tournament):
    """(slug, version) to store on the participant; legacy version when no terms."""
    terms = tournament.terms
    if terms is None:
        return "", tournament.terms_version
    return terms.slug, terms.version


def accepted_terms(tournament, user):
    """The terms record *user* accepted for *tournament*, for the confirmation PDF.

    Looked up by the stored slug and version (even if the record is now inactive);
    falls back to the tournament's current terms only when nothing is stored.
    None means the legacy, record-less output.
    """
    from .models import TournamentParticipant, TournamentTerms

    participant = TournamentParticipant.objects.filter(
        tournament=tournament, user=user,
    ).first()
    if participant is not None and participant.accepted_terms_slug:
        record = TournamentTerms.objects.filter(
            slug=participant.accepted_terms_slug,
            version=participant.terms_version_accepted,
        ).first()
        if record is not None:
            return record
        log.warning(
            "accepted terms %s v%s not found for user=%s tournament=%s; using the tournament's terms",
            participant.accepted_terms_slug, participant.terms_version_accepted,
            user.pk, tournament.pk,
        )
    return tournament.terms
