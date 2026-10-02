"""Prize payout rules shared by the winner pages, the admin and the expiry task.

Order of steps: claim, legal check approved, winner confirms, AGL contacts, paid.
The legal-check gate applies only to tournaments that are not Israel-only. Hold and
block apply to every tournament but are off by default, so an Israel-only tournament
with nothing set behaves exactly as before.

Winner-facing text is deliberately neutral and never exposes internal reasons.
"""
from collections import namedtuple
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    EligibilityVerification, PayoutConfirmation, PrizeClaim, PrizeClaimEvent, Tournament,
)

HOLD_MESSAGE = "Your prize is on hold."
VERIFYING_MESSAGE = "We are still verifying your eligibility."

Blocker = namedtuple("Blocker", "code admin_message winner_message")

STEPS = ("hold", "blocked", "legal", "confirmed", "contacted")


def eligibility_for(claim):
    return (
        EligibilityVerification.objects
        .filter(tournament_entry__tournament_id=claim.tournament_id, tournament_entry__user_id=claim.winner_id)
        .first()
    )


def legal_check_state(claim):
    """'missing', 'pending', 'rejected', 'incomplete' (verified without who/when) or 'verified'."""
    check = eligibility_for(claim)
    if check is None:
        return "missing"
    if check.status == EligibilityVerification.Status.VERIFIED and not check.is_approved:
        return "incomplete"
    return check.status


def payout_confirmation_for(claim):
    return (
        PayoutConfirmation.objects
        .filter(tournament_entry__tournament_id=claim.tournament_id, tournament_entry__user_id=claim.winner_id)
        .first()
    )


def winner_confirmed(claim):
    confirmation = payout_confirmation_for(claim)
    return bool(confirmation and confirmation.confirmed_by_user)


def payout_blocker(claim, through="contacted"):
    """First step that stops the payout, looking only at steps up to and including *through*.

    Returns a Blocker (code, text for staff, text for the winner) or None.
    """
    tournament = claim.tournament
    wanted = STEPS[: STEPS.index(through) + 1]

    if "hold" in wanted and tournament.prize_on_hold:
        return Blocker("hold", "the prize is on hold", HOLD_MESSAGE)
    if "blocked" in wanted and claim.status == PrizeClaim.Status.BLOCKED:
        return Blocker("blocked", "the claim is blocked", HOLD_MESSAGE)
    if "legal" in wanted and tournament.requires_legal_check:
        state = legal_check_state(claim)
        if state != "verified":
            return Blocker("legal", f"legal check not approved (status: {state})", VERIFYING_MESSAGE)
    if "confirmed" in wanted and not winner_confirmed(claim):
        return Blocker("confirmed", "the winner has not confirmed yet", "")
    if "contacted" in wanted and claim.payout_method != Tournament.PayoutMethod.PAYPAL:
        confirmation = payout_confirmation_for(claim)
        if not (confirmation and confirmation.contacted_at):
            return Blocker(
                "contacted",
                "payment details have not been requested yet (use \"Record: payment details requested\")",
                "",
            )
    return None


def winner_notice(tournament, claim):
    """Neutral message for the winner when the prize cannot proceed, else ''."""
    if tournament.prize_on_hold or (claim is not None and claim.status == PrizeClaim.Status.BLOCKED):
        return HOLD_MESSAGE
    if tournament.requires_legal_check:
        if claim is None:
            return HOLD_MESSAGE
        if legal_check_state(claim) != "verified":
            return VERIFYING_MESSAGE
    return ""


def log_event(claim, kind, by=None, reason=""):
    return PrizeClaimEvent.objects.create(claim=claim, kind=kind, by=by, reason=reason)


# ── Expiry clock ──────────────────────────────────────────────────────────────

def paused_reason(claim):
    """Why the expiry clock is stopped, or None while the next step belongs to the winner."""
    if claim.status == PrizeClaim.Status.BLOCKED:
        return "blocked"
    if claim.tournament.prize_on_hold:
        return "hold"
    if claim.status == PrizeClaim.Status.CLAIMED and claim.tournament.requires_legal_check:
        if legal_check_state(claim) != "verified":
            return "legal"
        if winner_confirmed(claim):
            return "agl"     # AGL contacts the winner and pays; nothing left for the winner
    return None


_ACTIVE = (PrizeClaim.Status.PENDING, PrizeClaim.Status.CLAIMED, PrizeClaim.Status.BLOCKED)


def sync_expiry_clock(claim, now=None):
    """Stop or restart the expiry clock to match the current state. Returns 'paused', 'resumed' or None.

    Pausing records the moment; resuming pushes expires_at back by the paused time, so the
    winner keeps exactly the days that were left.
    """
    now = now or timezone.now()
    with transaction.atomic():
        fresh = PrizeClaim.objects.select_related("tournament").select_for_update().get(pk=claim.pk)
        if fresh.status not in _ACTIVE:
            return None
        reason = paused_reason(fresh)
        outcome = None
        if reason and fresh.expiry_paused_at is None:
            fresh.expiry_paused_at = now
            fresh.save(update_fields=["expiry_paused_at"])
            log_event(fresh, PrizeClaimEvent.Kind.EXPIRY_PAUSED, reason=reason)
            outcome = "paused"
        elif not reason and fresh.expiry_paused_at is not None:
            fresh.expires_at = fresh.expires_at + (now - fresh.expiry_paused_at)
            fresh.expiry_paused_at = None
            fresh.save(update_fields=["expires_at", "expiry_paused_at"])
            log_event(fresh, PrizeClaimEvent.Kind.EXPIRY_RESUMED)
            outcome = "resumed"
    if outcome:
        claim.refresh_from_db()
    return outcome


def sync_for_tournament(tournament):
    claim = PrizeClaim.objects.filter(tournament=tournament).first()
    if claim is not None:
        sync_expiry_clock(claim)


def sync_for_entry(entry):
    claim = PrizeClaim.objects.filter(tournament_id=entry.tournament_id, winner_id=entry.user_id).first()
    if claim is not None:
        sync_expiry_clock(claim)


# ── Staff actions ─────────────────────────────────────────────────────────────

class ActionRefused(Exception):
    """A staff action cannot be applied to this claim; the message says why."""


def block_claim(claim, by, reason):
    with transaction.atomic():
        fresh = PrizeClaim.objects.select_for_update().get(pk=claim.pk)
        if fresh.status not in (PrizeClaim.Status.PENDING, PrizeClaim.Status.CLAIMED):
            raise ActionRefused(f"only pending or claimed claims can be blocked (status: {fresh.status})")
        fresh.status_before_block = fresh.status
        fresh.status = PrizeClaim.Status.BLOCKED
        fresh.blocked_reason = reason
        fresh.blocked_at = timezone.now()
        fresh.blocked_by = by
        fresh.save(update_fields=["status", "status_before_block", "blocked_reason", "blocked_at", "blocked_by"])
        log_event(fresh, PrizeClaimEvent.Kind.BLOCKED, by, reason)
    sync_expiry_clock(fresh)
    claim.refresh_from_db()


def unblock_claim(claim, by, reason):
    with transaction.atomic():
        fresh = PrizeClaim.objects.select_for_update().get(pk=claim.pk)
        if fresh.status != PrizeClaim.Status.BLOCKED:
            raise ActionRefused(f"the claim is not blocked (status: {fresh.status})")
        fresh.status = fresh.status_before_block or PrizeClaim.Status.CLAIMED
        fresh.status_before_block = ""
        fresh.blocked_reason = ""
        fresh.blocked_at = None
        fresh.blocked_by = None
        fresh.save(update_fields=["status", "status_before_block", "blocked_reason", "blocked_at", "blocked_by"])
        log_event(fresh, PrizeClaimEvent.Kind.UNBLOCKED, by, reason)
    sync_expiry_clock(fresh)
    claim.refresh_from_db()


def record_contact(claim, by):
    """AGL asked the winner (by email) for payment details."""
    if claim.status != PrizeClaim.Status.CLAIMED:
        raise ActionRefused(f"only claimed claims can be updated (status: {claim.status})")
    if claim.payout_method == Tournament.PayoutMethod.PAYPAL:
        raise ActionRefused("this prize is paid by PayPal; there is no separate contact step")
    blocker = payout_blocker(claim, through="confirmed")
    if blocker:
        raise ActionRefused(blocker.admin_message)
    confirmation = payout_confirmation_for(claim)
    if confirmation.contacted_at:
        raise ActionRefused("already recorded")
    confirmation.contacted_at = timezone.now()
    confirmation.contacted_by = by
    confirmation.save(update_fields=["contacted_at", "contacted_by"])
    log_event(claim, PrizeClaimEvent.Kind.CONTACTED, by)


def record_payment(claim, by, amount_paid, tax_withheld, payment_reference):
    if claim.status not in (PrizeClaim.Status.CLAIMED, PrizeClaim.Status.PAID):
        raise ActionRefused(f"only claimed or paid claims can be updated (status: {claim.status})")
    claim.amount_paid = amount_paid
    claim.tax_withheld = tax_withheld
    claim.payment_reference = payment_reference
    claim.save(update_fields=["amount_paid", "tax_withheld", "payment_reference"])
    log_event(claim, PrizeClaimEvent.Kind.PAYMENT_RECORDED, by)


def add_note(claim, by, note):
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    claim.admin_notes = f"{claim.admin_notes}\n[{stamp} {by.get_username()}] {note}".strip()
    claim.save(update_fields=["admin_notes"])
    log_event(claim, PrizeClaimEvent.Kind.NOTE, by, note)
