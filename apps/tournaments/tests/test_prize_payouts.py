from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.tournaments import payouts
from apps.tournaments.models import (
    EligibilityVerification, PayoutConfirmation, PrizeClaim, PrizeClaimEvent, Tournament,
    TournamentParticipant,
)
from apps.tournaments.tasks import expire_stale_prize_claims

User = get_user_model()
HOLD = "Your prize is on hold."
VERIFYING = "We are still verifying your eligibility."


class ClaimTestBase(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.winner = User.objects.create_user("winner", password="pw", email="winner@example.com")
        self.winner.paypal_email = "winner@paypal.test"
        self.winner.save(update_fields=["paypal_email"])

    def make(self, countries=("IL",), method="paypal", status=PrizeClaim.Status.CLAIMED, verified=None,
             confirmed=False, contacted=False, hold=False, days=30):
        """Create tournament + champion entry + claim (+ optional EV state and confirmation)."""
        countries = list(countries)
        t = Tournament.objects.create(
            name=f"T{Tournament.objects.count()}",
            type=Tournament.Type.GAUNTLET if countries == ["IL"] else Tournament.Type.GLADIATORMANIA,
            status=Tournament.Status.COMPLETED, game_type="chess", champion=self.winner,
            start_time=timezone.now(), is_money_tournament=True,
            prize_amount=Decimal("500.00"), prize_currency="ILS" if countries == ["IL"] else "INR",
            allowed_countries=countries, payout_method=method, prize_on_hold=hold,
        )
        entry = TournamentParticipant.objects.create(
            tournament=t, user=self.winner, paypal_email="winner@paypal.test",
            accepted_terms_slug="india-terms", terms_version_accepted="1.0",
        )
        now = timezone.now()
        claim = PrizeClaim.objects.create(
            tournament=t, winner=self.winner, amount=t.prize_amount, currency=t.prize_currency,
            payout_method=method, paypal_email="winner@paypal.test" if method == "paypal" else "",
            claim_code=f"code-{t.pk}", status=status, created_at=now, expires_at=now + timedelta(days=days),
            claimed_at=now if status == PrizeClaim.Status.CLAIMED else None,
        )
        ev = EligibilityVerification.objects.create(tournament_entry=entry)
        if verified == "verified":
            ev.status, ev.verified_by, ev.verified_at = "verified", self.staff, now
        elif verified == "rejected":
            ev.status, ev.verified_by, ev.verified_at, ev.notes = "rejected", self.staff, now, "not eligible"
        elif verified == "unstamped":
            ev.status = "verified"
        ev.save()
        if confirmed:
            PayoutConfirmation.objects.create(
                tournament_entry=entry, confirmed_by_user=True, confirmed_at=now, payout_method=method,
                contacted_at=now if contacted else None,
            )
        return t, claim

    def paid(self, claim):
        claim.refresh_from_db()
        return claim.status == PrizeClaim.Status.PAID

    def admin_action(self, claim, action, **data):
        self.client.force_login(self.staff)
        return self.client.post(
            reverse("admin:tournaments_prizeclaim_changelist"),
            {"action": action, "_selected_action": [claim.pk], **data}, follow=True,
        )


def _messages(resp):
    return [str(m) for m in resp.context["messages"]]


class IsraelOnlyUnchangedTest(ClaimTestBase):
    """Pins: with nothing set, an Israel-only prize behaves exactly as before."""

    def test_mark_as_paid_messages_and_rules(self):
        t, claim = self.make(["IL"], confirmed=False)
        resp = self.admin_action(claim, "mark_as_paid")
        self.assertIn("1 claim(s) blocked — winner has not confirmed their payout address yet.", _messages(resp))
        self.assertFalse(self.paid(claim))

        PayoutConfirmation.objects.create(
            tournament_entry=TournamentParticipant.objects.get(tournament=t), confirmed_by_user=True,
            paypal_email_snapshot="winner@paypal.test",
        )
        resp = self.admin_action(claim, "mark_as_paid")
        self.assertEqual(_messages(resp), ["Marked 1 claim(s) as paid."])
        self.assertTrue(self.paid(claim))
        t.refresh_from_db()
        self.assertEqual(t.payout_status, Tournament.PayoutStatus.PAID)

        resp = self.admin_action(claim, "mark_as_paid")
        self.assertEqual(_messages(resp), ["1 claim(s) skipped — only CLAIMED items can be marked as paid."])

    def test_no_legal_check_needed_for_israel_only(self):
        t, claim = self.make(["IL"], verified=None, confirmed=True)
        self.admin_action(claim, "mark_as_paid")
        self.assertTrue(self.paid(claim))

    def test_paypal_claim_and_confirm_pages(self):
        t, claim = self.make(["IL"], status=PrizeClaim.Status.PENDING)
        self.client.force_login(self.winner)
        page = self.client.get(reverse("tournaments:prize_claim", args=[t.pk]))
        self.assertContains(page, "PayPal Payout Address")
        self.assertNotContains(page, "How to be paid")
        resp = self.client.post(reverse("tournaments:prize_claim", args=[t.pk]), {"paypal_email": "new@paypal.test"})
        self.assertContains(resp, "Claim received.")
        self.assertContains(resp, "via PayPal once approved")
        self.assertContains(resp, "No further action is required.")
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.paypal_email), ("claimed", "new@paypal.test"))
        self.assertTrue(any("PayPal address on file: new@paypal.test" in m.body for m in mail.outbox))
        self.assertTrue(any("PayPal     : new@paypal.test" in m.body for m in mail.outbox))

        confirm = self.client.get(reverse("tournaments:payout_confirm", args=[t.pk]))
        self.assertContains(confirm, "Confirm — send prize to this address")
        self.client.post(reverse("tournaments:payout_confirm", args=[t.pk]))
        self.assertTrue(payouts.winner_confirmed(claim))
        self.assertEqual(claim.events.count(), 0)

    def test_expiry_task_unchanged(self):
        t, claim = self.make(["IL"], days=-1)
        self.assertEqual(expire_stale_prize_claims(), "Expired 1 claim(s).")
        claim.refresh_from_db()
        self.assertEqual(claim.status, PrizeClaim.Status.EXPIRED)
        self.assertEqual(claim.events.count(), 0)
        t.refresh_from_db()
        self.assertEqual(t.payout_status, Tournament.PayoutStatus.CANCELLED)
        self.assertEqual(expire_stale_prize_claims(), "No claims to expire.")

    def test_claim_clock_never_pauses_for_israel_only(self):
        for status in (PrizeClaim.Status.PENDING, PrizeClaim.Status.CLAIMED):
            t, claim = self.make(["IL"], status=status)
            self.assertIsNone(payouts.paused_reason(claim))
            self.assertIsNone(payouts.sync_expiry_clock(claim))


class SensitiveGuardTest(ClaimTestBase):
    def test_free_text_fields_reject_clear_matches(self):
        t, claim = self.make(["IN"])
        for field, value in (
            ("admin_notes", "paid to ramesh@okhdfcbank"),
            ("payment_reference", "ABCDE1234F"),
            ("blocked_reason", "acct 123456789012"),
        ):
            with self.subTest(field=field):
                setattr(claim, field, value)
                with self.assertRaises(ValidationError) as ctx:
                    claim.full_clean(exclude=["tournament", "winner", "claim_code"])
                self.assertIn(field, ctx.exception.message_dict)
                setattr(claim, field, "")
        ev = EligibilityVerification.objects.get()
        ev.notes = "passport 123456789"
        with self.assertRaises(ValidationError) as ctx:
            ev.full_clean(exclude=["tournament_entry"])
        self.assertIn("notes", ctx.exception.message_dict)


class EligibilityRulesTest(ClaimTestBase):
    def test_verified_needs_who_and_when_and_rejected_needs_a_note(self):
        t, claim = self.make(["IN"])
        ev = EligibilityVerification.objects.get()
        ev.status = "verified"
        with self.assertRaises(ValidationError) as ctx:
            ev.full_clean(exclude=["tournament_entry"])
        self.assertIn("status", ctx.exception.message_dict)
        ev.status = "rejected"
        with self.assertRaises(ValidationError) as ctx:
            ev.full_clean(exclude=["tournament_entry"])
        self.assertIn("notes", ctx.exception.message_dict)
        ev.notes = "residency not confirmed"
        ev.full_clean(exclude=["tournament_entry"])

    def test_admin_stamps_who_and_when(self):
        t, claim = self.make(["IN"])
        ev = EligibilityVerification.objects.get()
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("admin:tournaments_eligibilityverification_change", args=[ev.pk]),
            {"status": "verified", "verification_method": "video call", "notes": ""},
        )
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["adminform"].form.errors)
        ev.refresh_from_db()
        self.assertTrue(ev.is_approved)
        self.assertEqual(ev.verified_by, self.staff)
        # Going back to pending clears the stamp.
        self.client.post(
            reverse("admin:tournaments_eligibilityverification_change", args=[ev.pk]),
            {"status": "pending", "verification_method": "video call", "notes": ""},
        )
        ev.refresh_from_db()
        self.assertEqual((ev.verified_by, ev.verified_at), (None, None))

    def test_admin_rejects_a_sensitive_note(self):
        t, claim = self.make(["IN"])
        ev = EligibilityVerification.objects.get()
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("admin:tournaments_eligibilityverification_change", args=[ev.pk]),
            {"status": "rejected", "verification_method": "", "notes": "PAN ABCDE1234F mismatch"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("looks like it contains", str(resp.context["adminform"].form.errors["notes"]))
        ev.refresh_from_db()
        self.assertEqual(ev.status, "pending")

    def test_verified_without_reviewer_is_not_approved(self):
        t, claim = self.make(["IN"], verified="unstamped")
        self.assertEqual(payouts.legal_check_state(claim), "incomplete")


class MarkAsPaidGateTest(ClaimTestBase):
    def refused(self, claim):
        resp = self.admin_action(claim, "mark_as_paid")
        self.assertFalse(self.paid(claim))
        return " ".join(_messages(resp))

    def test_each_step_refuses_with_a_clear_staff_message_in_order(self):
        t, claim = self.make(["IN"], method="bank_transfer", verified=None)
        label = f"Claim #{claim.pk} ({t.name}):"

        self.assertIn(f"{label} legal check not approved (status: pending).", self.refused(claim))
        ev = EligibilityVerification.objects.get()
        ev.status, ev.verified_by, ev.verified_at = "rejected", self.staff, timezone.now()
        ev.notes = "no"
        ev.save()
        self.assertIn("legal check not approved (status: rejected)", self.refused(claim))

        ev.status = "verified"
        ev.save()
        self.assertIn("the winner has not confirmed yet", self.refused(claim))

        entry = TournamentParticipant.objects.get()
        PayoutConfirmation.objects.create(tournament_entry=entry, confirmed_by_user=True, confirmed_at=timezone.now())
        self.assertIn("payment details have not been requested yet", self.refused(claim))

        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=True)
        self.assertIn(f"{label} the prize is on hold.", self.refused(claim))
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=False)

        self.admin_action(claim, "record_contact")
        resp = self.admin_action(claim, "mark_as_paid")
        self.assertEqual(_messages(resp), ["Marked 1 claim(s) as paid."])
        self.assertTrue(self.paid(claim))

    def test_missing_legal_check_row_is_refused(self):
        t, claim = self.make(["IN"], confirmed=True)
        EligibilityVerification.objects.all().delete()
        self.assertIn("legal check not approved (status: missing)", self.refused(claim))

    def test_paypal_outside_israel_needs_legal_check_and_confirmation_but_no_contact_step(self):
        t, claim = self.make(["IN"], method="paypal", verified="verified", confirmed=True)
        resp = self.admin_action(claim, "mark_as_paid")
        self.assertEqual(_messages(resp), ["Marked 1 claim(s) as paid."])

    def test_hold_and_block_also_stop_israel_only_claims(self):
        t, claim = self.make(["IL"], confirmed=True, hold=True)
        self.assertIn("the prize is on hold", self.refused(claim))
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=False)
        payouts.block_claim(claim, self.staff, "checking")
        resp = self.admin_action(claim, "mark_as_paid")
        self.assertEqual(_messages(resp), ["1 claim(s) skipped — only CLAIMED items can be marked as paid."])

    def test_non_paypal_israel_only_still_needs_the_contact_step(self):
        t, claim = self.make(["IL"], method="upi", confirmed=True)
        self.assertIn("payment details have not been requested yet", self.refused(claim))


class WinnerPagesTest(ClaimTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.winner)

    def confirm_url(self, t):
        return reverse("tournaments:payout_confirm", args=[t.pk])

    def claim_url(self, t):
        return reverse("tournaments:prize_claim", args=[t.pk])

    def test_confirm_page_is_neutral_until_the_legal_check_is_approved(self):
        t, claim = self.make(["IN"], method="bank_transfer", verified=None)
        page = self.client.get(self.confirm_url(t))
        self.assertContains(page, VERIFYING)
        self.assertNotContains(page, "Confirm the prize terms")
        self.client.post(self.confirm_url(t))
        self.assertFalse(payouts.winner_confirmed(claim))
        for text in ("pending", "rejected", "legal"):
            self.assertNotContains(page, text)

    def test_rejected_check_looks_the_same_to_the_winner(self):
        t, claim = self.make(["IN"], method="bank_transfer", verified="rejected")
        page = self.client.get(self.confirm_url(t))
        self.assertContains(page, VERIFYING)
        self.assertNotContains(page, "not eligible")

    def test_hold_and_block_show_only_the_neutral_message(self):
        t, claim = self.make(["IN"], method="bank_transfer", verified="verified", hold=True)
        Tournament.objects.filter(pk=t.pk).update(prize_hold_reason="counsel review ongoing")
        for url in (self.confirm_url(t), self.claim_url(t)):
            page = self.client.get(url)
            self.assertContains(page, HOLD)
            self.assertNotContains(page, "counsel")
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=False)
        payouts.block_claim(claim, self.staff, "internal reason xyz")
        for url in (self.confirm_url(t), self.claim_url(t)):
            page = self.client.get(url)
            self.assertContains(page, HOLD)
            self.assertNotContains(page, "xyz")

    def test_hold_refuses_claiming_too(self):
        t, claim = self.make(["IL"], status=PrizeClaim.Status.PENDING, hold=True)
        resp = self.client.post(self.claim_url(t), {"paypal_email": "x@y.test"})
        self.assertContains(resp, HOLD)
        claim.refresh_from_db()
        self.assertEqual(claim.status, PrizeClaim.Status.PENDING)

    def test_non_paypal_claim_needs_an_account_email(self):
        t, claim = self.make(["IN"], method="upi", status=PrizeClaim.Status.PENDING)
        self.winner.email = ""
        self.winner.save(update_fields=["email"])
        page = self.client.get(self.claim_url(t))
        self.assertContains(page, "Add and confirm an email address on your account first")
        self.assertContains(page, "tab=edit")
        resp = self.client.post(self.claim_url(t))
        self.assertContains(resp, "Add and confirm an email address")
        claim.refresh_from_db()
        self.assertEqual(claim.status, PrizeClaim.Status.PENDING)

    def test_non_paypal_claim_has_no_paypal_box_and_stores_nothing_but_the_method(self):
        t, claim = self.make(["IN"], method="upi", status=PrizeClaim.Status.PENDING)
        Tournament.objects.filter(pk=t.pk).update(payout_instructions="We email you after verification.")
        page = self.client.get(self.claim_url(t))
        self.assertNotContains(page, "PayPal")
        self.assertContains(page, "We email you after verification.")
        resp = self.client.post(self.claim_url(t), {"paypal_email": "sneaky@paypal.test"})
        self.assertContains(resp, "Claim received.")
        self.assertNotContains(resp, "PayPal")
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.paypal_email), ("claimed", ""))
        self.assertTrue(any("Payout method: UPI" in m.body for m in mail.outbox))
        self.assertTrue(any("Method     : UPI" in m.body for m in mail.outbox))
        self.assertFalse(any("PayPal" in m.body for m in mail.outbox if "claim received" in m.subject.lower()))

    def test_claim_page_after_claiming_shows_neutral_progress(self):
        t, claim = self.make(["IN"], method="upi", verified=None)
        page = self.client.get(self.claim_url(t))
        self.assertContains(page, VERIFYING)
        self.assertNotContains(page, "Confirm your prize details")
        ev = EligibilityVerification.objects.get()
        ev.status, ev.verified_by, ev.verified_at = "verified", self.staff, timezone.now()
        ev.save()
        page = self.client.get(self.claim_url(t))
        self.assertNotContains(page, VERIFYING)
        self.assertContains(page, "Confirm your prize details")

    def test_non_paypal_confirmation_records_terms_and_contact_not_account_details(self):
        t, claim = self.make(["IN"], method="bank_transfer", verified="verified")
        page = self.client.get(self.confirm_url(t))
        self.assertContains(page, "Confirm the prize terms")
        self.assertContains(page, "Never send bank or UPI details")
        self.client.post(self.confirm_url(t))
        c = PayoutConfirmation.objects.get()
        self.assertTrue(c.confirmed_by_user)
        self.assertEqual(
            (c.payout_method, c.contact_email_snapshot, c.terms_slug, c.terms_version, c.paypal_email_snapshot),
            ("bank_transfer", "winner@example.com", "india-terms", "1.0", ""),
        )
        # A later email change does not alter the snapshot, and re-posting does not overwrite it.
        self.winner.email = "other@example.com"
        self.winner.save(update_fields=["email"])
        self.client.post(self.confirm_url(t))
        c.refresh_from_db()
        self.assertEqual(c.contact_email_snapshot, "winner@example.com")

    def test_non_paypal_confirmation_without_email_redirects_to_the_profile(self):
        t, claim = self.make(["IN"], method="upi", verified="verified")
        self.winner.email = ""
        self.winner.save(update_fields=["email"])
        resp = self.client.post(self.confirm_url(t))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=edit", resp["Location"])
        self.assertFalse(payouts.winner_confirmed(claim))


class BlockUnblockActionsTest(ClaimTestBase):
    def test_block_needs_a_reason_and_records_who_when_why(self):
        t, claim = self.make(["IN"], method="upi", verified="verified")
        self.client.force_login(self.staff)
        url = reverse("admin:tournaments_prizeclaim_changelist")
        shown = self.client.post(url, {"action": "block_claims", "_selected_action": [claim.pk]})
        self.assertContains(shown, "Block claims")
        empty = self.client.post(url, {"action": "block_claims", "_selected_action": [claim.pk], "apply": "1", "reason": ""})
        self.assertContains(empty, "This field is required")
        claim.refresh_from_db()
        self.assertEqual(claim.status, PrizeClaim.Status.CLAIMED)

        sensitive = self.client.post(url, {"action": "block_claims", "_selected_action": [claim.pk], "apply": "1", "reason": "see ramesh@okhdfcbank"})
        self.assertContains(sensitive, "looks like it contains")

        self.client.post(url, {"action": "block_claims", "_selected_action": [claim.pk], "apply": "1", "reason": "sanctions screening"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, PrizeClaim.Status.BLOCKED)
        self.assertEqual((claim.blocked_reason, claim.blocked_by, claim.status_before_block), ("sanctions screening", self.staff, "claimed"))
        self.assertIsNotNone(claim.blocked_at)
        event = claim.events.get(kind="blocked")
        self.assertEqual((event.by, event.reason), (self.staff, "sanctions screening"))

        self.client.post(url, {"action": "unblock_claims", "_selected_action": [claim.pk], "apply": "1", "reason": "cleared"})
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.blocked_reason, claim.status_before_block), ("claimed", "", ""))
        self.assertEqual(claim.events.get(kind="unblocked").reason, "cleared")

    def test_unblock_restores_pending_and_wrong_states_are_refused(self):
        t, claim = self.make(["IN"], status=PrizeClaim.Status.PENDING)
        payouts.block_claim(claim, self.staff, "x")
        payouts.unblock_claim(claim, self.staff, "y")
        self.assertEqual(claim.status, PrizeClaim.Status.PENDING)
        with self.assertRaises(payouts.ActionRefused):
            payouts.unblock_claim(claim, self.staff, "again")
        claim.status = PrizeClaim.Status.PAID
        claim.save(update_fields=["status"])
        with self.assertRaises(payouts.ActionRefused):
            payouts.block_claim(claim, self.staff, "late")

    def test_refusal_is_shown_per_claim(self):
        t, claim = self.make(["IN"], status=PrizeClaim.Status.PAID)
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("admin:tournaments_prizeclaim_changelist"),
            {"action": "block_claims", "_selected_action": [claim.pk], "apply": "1", "reason": "why"}, follow=True,
        )
        self.assertIn(f"Claim #{claim.pk} ({t.name}): only pending or claimed claims can be blocked (status: paid).", _messages(resp))

    def test_blocked_claim_page_for_the_admin_lists_events(self):
        t, claim = self.make(["IN"])
        payouts.block_claim(claim, self.staff, "screening")
        self.client.force_login(self.staff)
        page = self.client.get(reverse("admin:tournaments_prizeclaim_change", args=[claim.pk]))
        self.assertContains(page, "screening")
        self.assertContains(page, "Blocked")


class RecordActionsTest(ClaimTestBase):
    def test_contact_requires_the_earlier_steps(self):
        t, claim = self.make(["IN"], method="upi", verified=None)
        resp = self.admin_action(claim, "record_contact")
        self.assertIn(f"Claim #{claim.pk} ({t.name}): legal check not approved (status: pending).", _messages(resp))
        ev = EligibilityVerification.objects.get()
        ev.status, ev.verified_by, ev.verified_at = "verified", self.staff, timezone.now()
        ev.save()
        resp = self.admin_action(claim, "record_contact")
        self.assertIn("the winner has not confirmed yet", " ".join(_messages(resp)))
        PayoutConfirmation.objects.create(tournament_entry=TournamentParticipant.objects.get(), confirmed_by_user=True)
        resp = self.admin_action(claim, "record_contact")
        self.assertEqual(_messages(resp), ["Recorded the contact step for 1 claim(s)."])
        c = PayoutConfirmation.objects.get()
        self.assertEqual(c.contacted_by, self.staff)
        self.assertIsNotNone(c.contacted_at)
        self.assertEqual(claim.events.filter(kind="contacted").count(), 1)
        resp = self.admin_action(claim, "record_contact")
        self.assertIn("already recorded", " ".join(_messages(resp)))

    def test_contact_is_not_a_paypal_step(self):
        t, claim = self.make(["IN"], method="paypal", verified="verified", confirmed=True)
        resp = self.admin_action(claim, "record_contact")
        self.assertIn("this prize is paid by PayPal", " ".join(_messages(resp)))

    def test_payment_details_are_recorded_and_guarded(self):
        t, claim = self.make(["IN"])
        self.client.force_login(self.staff)
        url = reverse("admin:tournaments_prizeclaim_changelist")
        base = {"action": "record_payment_details", "_selected_action": [claim.pk], "apply": "1"}
        bad = self.client.post(url, {**base, "amount_paid": "450", "tax_withheld": "50", "payment_reference": "ABCDE1234F"})
        self.assertContains(bad, "looks like it contains")
        self.client.post(url, {**base, "amount_paid": "450.00", "tax_withheld": "50.00", "payment_reference": "transfer 2026-10-03"})
        claim.refresh_from_db()
        self.assertEqual((claim.amount_paid, claim.tax_withheld, claim.payment_reference),
                         (Decimal("450.00"), Decimal("50.00"), "transfer 2026-10-03"))
        self.assertEqual(claim.events.filter(kind="payment_recorded").count(), 1)

    def test_notes_are_stamped_and_guarded(self):
        t, claim = self.make(["IN"])
        self.client.force_login(self.staff)
        url = reverse("admin:tournaments_prizeclaim_changelist")
        base = {"action": "add_note", "_selected_action": [claim.pk], "apply": "1"}
        self.assertContains(self.client.post(url, {**base, "note": "123456789012"}), "looks like it contains")
        self.client.post(url, {**base, "note": "Called winner"})
        claim.refresh_from_db()
        self.assertIn("boss] Called winner", claim.admin_notes)


class ExpiryClockTest(ClaimTestBase):
    def test_blocked_claim_pauses_and_resumes_with_the_days_it_had_left(self):
        t, claim = self.make(["IN"], status=PrizeClaim.Status.PENDING, days=30)
        original = claim.expires_at
        payouts.block_claim(claim, self.staff, "screening")
        claim.refresh_from_db()
        paused_at = claim.expiry_paused_at
        self.assertIsNotNone(paused_at)

        payouts.unblock_claim(claim, self.staff, "cleared")
        claim.refresh_from_db()
        self.assertIsNone(claim.expiry_paused_at)
        self.assertGreaterEqual(claim.expires_at, original)
        self.assertEqual(
            [e.kind for e in claim.events.all()],
            ["blocked", "expiry_paused", "unblocked", "expiry_resumed"],
        )

    def test_resume_adds_exactly_the_paused_time(self):
        t, claim = self.make(["IN"], status=PrizeClaim.Status.PENDING, days=30)
        t0 = timezone.now()
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=True)
        self.assertEqual(payouts.sync_expiry_clock(claim, now=t0), "paused")
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=False)
        claim.refresh_from_db()
        before = claim.expires_at
        self.assertEqual(payouts.sync_expiry_clock(claim, now=t0 + timedelta(days=12)), "resumed")
        claim.refresh_from_db()
        self.assertEqual(claim.expires_at - before, timedelta(days=12))
        self.assertIsNone(claim.expiry_paused_at)

    def test_task_does_not_expire_paused_claims_but_expires_running_ones(self):
        t1, held = self.make(["IN"], status=PrizeClaim.Status.PENDING, days=-5, hold=True)
        t2, running = self.make(["IN"], status=PrizeClaim.Status.PENDING, days=-5)
        self.assertEqual(expire_stale_prize_claims(), "Expired 1 claim(s).")
        held.refresh_from_db()
        running.refresh_from_db()
        self.assertEqual(held.status, PrizeClaim.Status.PENDING)
        self.assertEqual(running.status, PrizeClaim.Status.EXPIRED)
        self.assertIsNotNone(held.expiry_paused_at)

    def test_clock_runs_only_while_the_next_step_is_the_winners(self):
        # Pending: the winner has to claim -> running, even while the legal check is open.
        t, claim = self.make(["IN"], method="upi", status=PrizeClaim.Status.PENDING, verified=None)
        self.assertIsNone(payouts.paused_reason(claim))

        # Claimed, legal check open: AGL's turn -> paused.
        claim.status = PrizeClaim.Status.CLAIMED
        self.assertEqual(payouts.paused_reason(claim), "legal")

        # Verified, winner has not confirmed: winner's turn -> running.
        ev = EligibilityVerification.objects.get()
        ev.status, ev.verified_by, ev.verified_at = "verified", self.staff, timezone.now()
        ev.save()
        self.assertIsNone(payouts.paused_reason(claim))

        # Winner confirmed: AGL contacts and pays -> paused.
        PayoutConfirmation.objects.create(tournament_entry=TournamentParticipant.objects.get(), confirmed_by_user=True)
        self.assertEqual(payouts.paused_reason(claim), "agl")

    def test_verification_in_the_admin_resumes_the_clock(self):
        t, claim = self.make(["IN"], method="upi", verified=None)
        self.assertEqual(payouts.sync_expiry_clock(claim), "paused")
        ev = EligibilityVerification.objects.get()
        self.client.force_login(self.staff)
        self.client.post(
            reverse("admin:tournaments_eligibilityverification_change", args=[ev.pk]),
            {"status": "verified", "verification_method": "video call", "notes": ""},
        )
        claim.refresh_from_db()
        self.assertIsNone(claim.expiry_paused_at)
        self.assertEqual(claim.events.filter(kind="expiry_resumed").count(), 1)

    def test_winner_confirmation_pauses_the_clock(self):
        t, claim = self.make(["IN"], method="bank_transfer", verified="verified")
        self.client.force_login(self.winner)
        self.client.post(reverse("tournaments:payout_confirm", args=[t.pk]))
        claim.refresh_from_db()
        self.assertIsNotNone(claim.expiry_paused_at)

    def test_claiming_with_an_open_legal_check_pauses_the_clock(self):
        t, claim = self.make(["IN"], method="upi", status=PrizeClaim.Status.PENDING, verified=None)
        self.client.force_login(self.winner)
        self.client.post(reverse("tournaments:prize_claim", args=[t.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, PrizeClaim.Status.CLAIMED)
        self.assertIsNotNone(claim.expiry_paused_at)

    def test_hold_toggle_in_the_tournament_admin_syncs_the_clock(self):
        t, claim = self.make(["IN"], status=PrizeClaim.Status.PENDING)
        t.prize_on_hold = True
        t.save(update_fields=["prize_on_hold"])
        self.assertEqual(payouts.sync_expiry_clock(claim), "paused")
        # Releasing through the admin form resumes it.
        from django.contrib import admin as dj_admin
        from django.test import RequestFactory
        from apps.tournaments.admin import TournamentAdmin

        ma = TournamentAdmin(Tournament, dj_admin.site)
        request = RequestFactory().post("/")
        request.user = self.staff

        class FakeForm:
            changed_data = ["prize_on_hold"]

        t.prize_on_hold = False
        ma.save_model(request, t, FakeForm(), change=True)
        claim.refresh_from_db()
        self.assertIsNone(claim.expiry_paused_at)

    def test_pending_claim_alert_is_hidden_while_on_hold(self):
        from apps.core.context_processors import _get_prize_claim_alert
        t, claim = self.make(["IN"], status=PrizeClaim.Status.PENDING)
        self.assertIsNotNone(_get_prize_claim_alert(self.winner))
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=True)
        self.assertIsNone(_get_prize_claim_alert(self.winner))
