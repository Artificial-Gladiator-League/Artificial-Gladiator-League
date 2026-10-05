"""Profile country: early eligibility filter, join-time snapshot, staff disagreement signal."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.profile_country import (
    AGREE, MISMATCH, MISSING, NO_DATA, NOT_ALLOWED, NOT_ELIGIBLE_NOTICE, country_agreement,
    profile_country_verdict,
)
from apps.tournaments.tests.test_terms_join import PASS_GATES, JoinTermsTestBase

User = get_user_model()

ALL = {
    "terms_accepted": "1", "confirmed_age_18_plus": "1",
    "confirmed_israeli_resident": "1", "confirmed_residency": "1",
}


def _make(slug, ttype, countries, money=True):
    terms = TournamentTerms.objects.create(
        slug=slug, version="1.0", title=slug, body="<p>x</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    t = Tournament.objects.create(
        name=slug, type=ttype, status=Tournament.Status.OPEN,
        start_time=timezone.now() + timedelta(days=1), capacity=16, rounds_total=5,
        is_money_tournament=money, prize_amount=Decimal("500.00") if money else None,
        prize_currency="INR", allowed_countries=countries, terms=terms, terms_version="",
    )
    return t, terms


def _messages(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


class VerdictTest(TestCase):
    """The pure decision, without views."""

    def setUp(self):
        self.user = User.objects.create_user("v", password="pw")

    def verdict(self, tournament):
        return profile_country_verdict(User.objects.get(pk=self.user.pk), tournament)

    def test_israel_only_never_blocks_on_missing_country(self):
        t, _ = _make("v1", Tournament.Type.GAUNTLET, ["IL"])
        self.assertIsNone(self.verdict(t))

    def test_restricted_tournament_needs_a_country(self):
        t, _ = _make("v2", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.assertEqual(self.verdict(t).kind, MISSING)
        t2, _ = _make("v3", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.assertEqual(self.verdict(t2).kind, MISSING)

    def test_matching_country_passes(self):
        t, _ = _make("v4", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.user.claim_country("IL")
        self.assertIsNone(self.verdict(t))

    def test_other_country_is_blocked_with_names(self):
        t, _ = _make("v5", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.user.claim_country("US")
        verdict = self.verdict(t)
        self.assertEqual(verdict.kind, NOT_ALLOWED)
        self.assertEqual(verdict.message, NOT_ELIGIBLE_NOTICE)

    def test_free_israel_only_tournament_has_no_country_gate(self):
        t, _ = _make("v6", Tournament.Type.QA, ["IL"], money=False)
        self.user.claim_country("US")
        self.assertIsNone(self.verdict(t))


class EarlyFilterViewsTest(JoinTermsTestBase):
    def terms_url(self, t):
        return reverse("tournaments:money_terms", args=[t.pk])

    def join(self, t, terms, **kw):
        with PASS_GATES[0], PASS_GATES[1]:
            return self.client.post(
                reverse("tournaments:join", args=[t.pk]), data=self.shown(terms, **ALL), **kw,
            )

    def test_missing_country_redirects_terms_and_join_to_profile_settings(self):
        t, terms = _make("e1", Tournament.Type.GLADIATORMANIA, ["IN"])
        for resp in (self.client.get(self.terms_url(t)), self.join(t, terms)):
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/users/profile/", resp["Location"])
            self.assertIn("tab=edit", resp["Location"])
            self.assertIn(f"next=/tournaments/{t.pk}/terms/", resp["Location"])
        self.assertFalse(self.joined(t))

    def test_after_setting_the_country_the_terms_page_opens(self):
        t, terms = _make("e2", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.client.post(reverse("users:save_country"), {
            "action": "set", "country": "IN", "country_confirm": "1", "next": self.terms_url(t),
        })
        resp = self.client.get(self.terms_url(t))
        self.assertEqual(resp.status_code, 200)

    def test_wrong_country_is_stopped_at_the_terms_page_with_a_clear_message(self):
        t, terms = _make("e3", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.user.claim_country("US")
        resp = self.client.get(self.terms_url(t))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("tournaments:detail", args=[t.pk]))
        self.assertIn(NOT_ELIGIBLE_NOTICE, _messages(resp)[0])

    def test_wrong_country_cannot_join_even_with_a_crafted_post(self):
        t, terms = _make("e4", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.user.claim_country("US")
        resp = self.join(t, terms)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.joined(t))
        self.assertIn(NOT_ELIGIBLE_NOTICE, _messages(resp)[-1])

    def test_matching_country_joins_and_records_the_profile_country(self):
        t, terms = _make("e5", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.user.claim_country("IN")
        resp = self.join(t, terms)
        self.assertEqual(resp.status_code, 302)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual(row.profile_country_code, "IN")

    def test_israel_only_user_without_country_joins_exactly_as_before(self):
        t, terms = _make("e6", Tournament.Type.GAUNTLET, ["IL"])
        self.assertEqual(self.client.get(self.terms_url(t)).status_code, 200)
        self.join(t, terms)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual(row.profile_country_code, "")

    def test_israel_only_user_with_israel_in_profile_joins(self):
        t, terms = _make("e7", Tournament.Type.GAUNTLET, ["IL"])
        self.user.claim_country("IL")
        self.join(t, terms)
        self.assertTrue(self.joined(t))
        self.assertEqual(TournamentParticipant.objects.get(tournament=t).profile_country_code, "IL")

    def test_filter_does_not_replace_the_ip_gate(self):
        """Profile says India, IP says elsewhere: the existing geo gate still decides."""
        from unittest import mock
        from django.test import override_settings
        t, terms = _make("e8", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.user.claim_country("IN")
        with override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True), \
                mock.patch("apps.tournaments.eligibility.lookup_country", return_value="US"):
            with PASS_GATES[0], PASS_GATES[1]:
                resp = self.client.post(
                    reverse("tournaments:join", args=[t.pk]), data=self.shown(terms, **ALL),
                    HTTP_X_FORWARDED_FOR="194.90.0.1",
                )
        self.assertFalse(self.joined(t))
        self.assertIn("Your location does not qualify", _messages(resp)[-1])


class CountryAgreementTest(TestCase):
    def row(self, profile="", ip="", declared=""):
        return TournamentParticipant(
            profile_country_code=profile, join_country_code=ip, declared_residency_countries=declared,
        )

    def test_states(self):
        self.assertEqual(country_agreement(self.row("IN", "IN", "IN"))[0], AGREE)
        self.assertEqual(country_agreement(self.row("IN", "IL", "IN"))[0], MISMATCH)
        self.assertEqual(country_agreement(self.row("IN", "IN", "IL"))[0], MISMATCH)
        self.assertEqual(country_agreement(self.row("IL", "IL", "IL,IN"))[0], AGREE)
        self.assertEqual(country_agreement(self.row("IN"))[0], NO_DATA)
        self.assertEqual(country_agreement(self.row())[0], NO_DATA)

    def test_detail_names_all_three_signals(self):
        detail = country_agreement(self.row("IN", "IL", "IN"))[1]
        self.assertEqual(detail, "profile=IN, ip=IL, declared=IN")


class AdminSignalAndTextTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("boss", "b@example.com", "pw")
        self.t, _ = _make("a1", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.bad = User.objects.create_user("bad", password="pw")
        self.good = User.objects.create_user("good", password="pw")
        TournamentParticipant.objects.create(
            tournament=self.t, user=self.bad, profile_country_code="IN",
            join_country_code="IL", declared_residency_countries="IN",
        )
        TournamentParticipant.objects.create(
            tournament=self.t, user=self.good, profile_country_code="IN",
            join_country_code="IN", declared_residency_countries="IN",
        )

    def test_admin_changelist_flags_three_way_disagreement(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("admin:tournaments_tournamentparticipant_changelist"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertEqual(body.count("MISMATCH"), 1)
        self.assertIn("profile=IN, ip=IL, declared=IN", body)

    def test_admin_filter_shows_only_disagreeing_rows(self):
        self.client.force_login(self.staff)
        url = reverse("admin:tournaments_tournamentparticipant_changelist")
        resp = self.client.get(url + "?country_agreement=mismatch")
        self.assertEqual([p.user.username for p in resp.context["cl"].result_list], ["bad"])

    def test_signal_blocks_nothing_and_changes_nothing(self):
        self.assertEqual(TournamentParticipant.objects.filter(tournament=self.t).count(), 2)

    def test_list_and_detail_show_country_names_as_text_not_flags(self):
        self.t.status = Tournament.Status.OPEN
        self.t.save()
        self.client.force_login(self.good)
        for url in (reverse("tournaments:list"), reverse("tournaments:detail", args=[self.t.pk])):
            body = self.client.get(url).content.decode()
            self.assertIn("Open to residents of India and Israel", body, url)
            self.assertNotIn("flags/", body, url)
            self.assertNotIn("lp-flag", body.replace(".lp-flag", ""), url)

    def test_israel_only_free_tournament_shows_no_country_line(self):
        t, _ = _make("a2", Tournament.Type.QA, ["IL"], money=False)
        self.client.force_login(self.good)
        body = self.client.get(reverse("tournaments:detail", args=[t.pk])).content.decode()
        self.assertNotIn("Open to residents of", body)
