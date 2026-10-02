import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.engine import _create_prize_claim
from apps.tournaments.models import PrizeClaim, Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.sensitive import sensitive_matches, validate_no_sensitive_data
from apps.tournaments.tests.test_terms_join import JoinTermsTestBase

User = get_user_model()
ADMIN_JS = Path(__file__).resolve().parents[3] / "static" / "admin" / "js" / "tournament_type_fields.js"


def _terms(slug, **kw):
    defaults = dict(
        version="1.0", title=slug, body="<p>x</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(slug=slug, **defaults)


def _tournament(countries=("IL",), ttype=None, **kw):
    countries = list(countries)
    ttype = ttype or (Tournament.Type.GAUNTLET if countries == ["IL"] else Tournament.Type.GLADIATORMANIA)
    defaults = dict(
        name="PS", type=ttype, status=Tournament.Status.OPEN, game_type="chess",
        start_time=timezone.now() + timedelta(days=1), is_money_tournament=True,
        prize_amount=Decimal("500.00"), prize_currency="INR" if countries != ["IL"] else "ILS",
        allowed_countries=countries, terms_version="",
        prize_on_hold=countries != ["IL"],  # India test tournaments start with the prize on hold
    )
    defaults.update(kw)
    return Tournament(**defaults)


class SensitiveDataGuardTest(SimpleTestCase):
    def test_clear_matches_are_found(self):
        self.assertTrue(sensitive_matches("pay ramesh@okhdfcbank please"))
        self.assertTrue(sensitive_matches("PAN ABCDE1234F seen"))
        self.assertTrue(sensitive_matches("pan abcde1234f"))
        self.assertTrue(sensitive_matches("acct 123456789012"))
        self.assertTrue(sensitive_matches("123456789"))

    def test_ordinary_text_passes(self):
        for text in (
            "", "Reviewed by Dana on 2026-10-02 14:30", "contact a@example.com or b@mail.example.org.",
            "12345678 is only eight digits", "ABCDE12345 has no final letter", "ticket #42, batch 12-34-56",
        ):
            with self.subTest(text=text):
                self.assertEqual(sensitive_matches(text), [])
                validate_no_sensitive_data(text)

    def test_error_explains_why(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_no_sensitive_data("send to ramesh@okhdfcbank")
        message = ctx.exception.messages[0]
        self.assertIn("UPI-style ID", message)
        self.assertIn("never the numbers", message)


class CurrencyRulesTest(TestCase):
    def errors(self, tournament, field="prize_currency"):
        with self.assertRaises(ValidationError) as ctx:
            tournament.clean()
        return " ".join(ctx.exception.message_dict[field])

    def test_other_countries_need_an_explicit_non_ils_currency(self):
        terms = _terms("cr1")
        self.assertIn("Set the prize currency explicitly", self.errors(_tournament(["IN"], prize_currency="", terms=terms)))
        message = self.errors(_tournament(["IN"], prize_currency="ILS", terms=terms))
        self.assertIn("ILS is the Israeli default and cannot be used", message)
        self.assertIn("Unknown ISO 4217", self.errors(_tournament(["IN"], prize_currency="ABC", terms=terms)))

    def test_valid_currency_is_normalised(self):
        t = _tournament(["IN"], prize_currency="inr", terms=_terms("cr2"))
        t.clean()
        self.assertEqual(t.prize_currency, "INR")

    def test_rule_applies_when_the_terms_carry_a_prize_even_without_an_amount(self):
        t = _tournament(["IN"], prize_currency="", prize_amount=None, is_money_tournament=False, terms=_terms("cr3"))
        self.assertIn("Set the prize currency explicitly", self.errors(t))

    def test_no_prize_means_no_currency_rule(self):
        _tournament(["IN"], prize_currency="", prize_amount=None, is_money_tournament=False).clean()

    def test_israel_only_is_untouched(self):
        terms = _terms("cr4")
        _tournament(["IL"], prize_currency="ILS", terms=terms).clean()
        _tournament(["IL"], prize_currency="", terms=terms).clean()


class ChessLockTest(TestCase):
    def test_any_list_containing_india_is_chess_only(self):
        for countries in (["IN"], ["IN", "IL"], ["US", "IN"]):
            with self.subTest(countries=countries):
                t = _tournament(countries, game_type="breakthrough", terms=_terms("cl-" + "".join(countries)))
                with self.assertRaises(ValidationError) as ctx:
                    t.clean()
                self.assertIn("chess only", " ".join(ctx.exception.message_dict["game_type"]))

    def test_chess_is_fine_and_other_lists_are_not_locked(self):
        _tournament(["IN"], terms=_terms("cl-ok")).clean()
        _tournament(["IL"], game_type="breakthrough", terms=_terms("cl-il")).clean()
        _tournament(["US"], game_type="breakthrough", terms=_terms("cl-us")).clean()

    def test_admin_script_locks_the_game_type(self):
        js = ADMIN_JS.read_text(encoding="utf-8")
        self.assertIn("id_allowed_countries", js)
        self.assertIn("indexOf('IN')", js)
        self.assertIn("o.disabled = locked && o.value !== 'chess'", js)


class VerificationDocumentsTest(TestCase):
    def test_list_property_skips_blank_lines(self):
        t = _tournament(["IN"], verification_documents="Passport\n\n  Driving licence \nPAN\nProof of address\n")
        self.assertEqual(t.verification_documents_list, ["Passport", "Driving licence", "PAN", "Proof of address"])

    def test_aadhaar_cannot_be_requested(self):
        for text in ("Passport\nAadhaar card", "aadhar number"):
            with self.subTest(text=text):
                t = _tournament(["IN"], verification_documents=text, terms=_terms("vd-" + str(len(text))))
                with self.assertRaises(ValidationError) as ctx:
                    t.clean()
                self.assertIn("Aadhaar must not be requested", " ".join(ctx.exception.message_dict["verification_documents"]))

    def test_hold_reason_rejects_sensitive_numbers(self):
        t = _tournament(["IN"], prize_hold_reason="awaiting ABCDE1234F", terms=_terms("vd-hold"))
        with self.assertRaises(ValidationError) as ctx:
            t.full_clean(exclude=["name"])
        self.assertIn("prize_hold_reason", ctx.exception.message_dict)


class PaypalRuleByPayoutMethodTest(JoinTermsTestBase):
    paypal = ""
    profile_country = "IN"   # every tournament here is India-only
    ALL = {"terms_accepted": "1", "confirmed_age_18_plus": "1", "confirmed_residency": "1"}

    def _make(self, slug, countries, **kw):
        terms = _terms(slug)
        t = _tournament(countries, terms=terms, capacity=16, rounds_total=5, **kw)
        t.save()
        return t, terms

    def test_paypal_is_required_only_for_paypal_payouts(self):
        from apps.tournaments.terms import paypal_email_required
        for method, expected in (("paypal", True), ("bank_transfer", False), ("upi", False), ("other", False)):
            with self.subTest(method=method):
                self.assertEqual(paypal_email_required(_tournament(["IN"], payout_method=method, terms=_terms("pm-" + method))), expected)
        no_prize = _tournament(["IL"], terms=_terms("pm-np", has_prize=False))
        self.assertFalse(paypal_email_required(no_prize))

    def test_join_without_paypal_email_for_bank_transfer(self):
        t, terms = self._make("pm-join", ["IN"], payout_method="bank_transfer")
        self.assertEqual(self.post_join(t, self.shown(terms, **self.ALL)).status_code, 302)
        self.assertTrue(self.joined(t))

    def test_join_without_paypal_email_still_refused_for_paypal(self):
        t, terms = self._make("pm-pp", ["IN"], payout_method="paypal")
        self.assertRejected(self.post_join(t, self.shown(terms, **self.ALL)), t)

    def test_terms_page_boxes(self):
        user = self.user
        user.paypal_email = "x@paypal.test"
        user.save(update_fields=["paypal_email"])
        t, _ = self._make("pm-page", ["IN"], payout_method="upi")
        resp = self.client.get(reverse("tournaments:money_terms", args=[t.pk]))
        self.assertNotContains(resp, "Payout address on file")
        self.assertContains(resp, "How the prize is paid: UPI")
        self.assertContains(resp, "AGL will contact you at your account email")

        t2, _ = self._make("pm-page2", ["IN"], payout_method="bank_transfer", payout_instructions="We will email you.\nNo details on the site.")
        resp = self.client.get(reverse("tournaments:money_terms", args=[t2.pk]))
        self.assertContains(resp, "We will email you.")
        self.assertNotContains(resp, "AGL will contact you at your account email")

        t3, _ = self._make("pm-page3", ["IN"], payout_method="paypal")
        resp = self.client.get(reverse("tournaments:money_terms", args=[t3.pk]))
        self.assertContains(resp, "Payout address on file")
        self.assertNotContains(resp, "How the prize is paid")


@override_settings(ADMINS=[("Admin", "admin@example.com")])
class ClaimCreationTest(TestCase):
    def setUp(self):
        self.champion = User.objects.create_user("champ", password="x", email="champ@example.com")
        self.champion.paypal_email = "champ@paypal.test"
        self.champion.save(update_fields=["paypal_email"])

    def _completed(self, countries, **kw):
        t = _tournament(countries, status=Tournament.Status.COMPLETED, champion=self.champion, **kw)
        t.save()
        TournamentParticipant.objects.create(tournament=t, user=self.champion, paypal_email="champ@paypal.test")
        return t

    def test_israel_only_claim_is_exactly_as_before(self):
        t = self._completed(["IL"], prize_currency="")
        _create_prize_claim(t, self.champion)
        claim = PrizeClaim.objects.get(tournament=t)
        self.assertEqual((claim.currency, claim.amount, claim.paypal_email), ("ILS", Decimal("500.00"), "champ@paypal.test"))
        self.assertAlmostEqual((claim.expires_at - claim.created_at).total_seconds(), 30 * 86400, delta=5)
        t.refresh_from_db()
        self.assertEqual(t.payout_status, Tournament.PayoutStatus.PROCESSING)

    def test_claim_deadline_days_is_used(self):
        t = self._completed(["IN"], prize_on_hold=False, claim_deadline_days=45)
        _create_prize_claim(t, self.champion)
        claim = PrizeClaim.objects.get(tournament=t)
        self.assertAlmostEqual((claim.expires_at - claim.created_at).total_seconds(), 45 * 86400, delta=5)

    def test_prize_on_hold_creates_no_claim(self):
        for countries in (["IL"], ["IN"]):
            with self.subTest(countries=countries):
                t = self._completed(countries, prize_on_hold=True)
                _create_prize_claim(t, self.champion)
                self.assertFalse(PrizeClaim.objects.filter(tournament=t).exists())
                t.refresh_from_db()
                self.assertNotEqual(t.payout_status, Tournament.PayoutStatus.PROCESSING)

    def test_non_israeli_tournament_without_a_real_currency_creates_no_claim_and_alerts_admins(self):
        for currency in ("", "ILS"):
            with self.subTest(currency=currency):
                mail.outbox.clear()
                t = self._completed(["IN"], prize_on_hold=False, prize_currency=currency)
                _create_prize_claim(t, self.champion)
                self.assertFalse(PrizeClaim.objects.filter(tournament=t).exists())
                self.assertTrue(any("Prize currency missing" in m.subject for m in mail.outbox))


class BackfillCommandTest(TestCase):
    def setUp(self):
        self.champion = User.objects.create_user("bchamp", password="x")

    def _completed(self, countries, **kw):
        t = _tournament(countries, status=Tournament.Status.COMPLETED, champion=self.champion, **kw)
        t.save()
        TournamentParticipant.objects.create(tournament=t, user=self.champion)
        return t

    def test_refuses_while_the_prize_is_on_hold_then_works_after_release(self):
        t = self._completed(["IN"], prize_on_hold=True, claim_deadline_days=60)
        with self.assertRaisesMessage(CommandError, "prize on hold"):
            call_command("backfill_prize_claim", t.pk)
        Tournament.objects.filter(pk=t.pk).update(prize_on_hold=False)
        call_command("backfill_prize_claim", t.pk)
        claim = PrizeClaim.objects.get(tournament=t)
        self.assertAlmostEqual((claim.expires_at - timezone.now()).total_seconds(), 60 * 86400, delta=30)

    def test_reports_an_error_when_no_claim_could_be_created(self):
        t = self._completed(["IN"], prize_on_hold=False, prize_currency="")
        with self.assertRaisesMessage(CommandError, "No PrizeClaim was created"):
            call_command("backfill_prize_claim", t.pk)

    def test_israel_only_backfill_is_unchanged(self):
        t = self._completed(["IL"])
        call_command("backfill_prize_claim", t.pk)
        claim = PrizeClaim.objects.get(tournament=t)
        self.assertEqual(claim.currency, "ILS")
        self.assertAlmostEqual((claim.expires_at - timezone.now()).total_seconds(), 30 * 86400, delta=30)
