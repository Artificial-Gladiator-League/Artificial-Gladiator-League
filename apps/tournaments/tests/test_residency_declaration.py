from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.terms import build_terms_context, residency_field_name
from apps.tournaments.tests.test_terms_join import PASS_GATES, JoinTermsTestBase

LOOKUP = "apps.tournaments.eligibility.lookup_country"
AGE_INDIA = "18 or the age of majority where I live, whichever is higher"
AGE_ISRAEL = "18 years of age or older"
LEGACY_RESIDENCY = "confirmed_israeli_resident"


def _make(slug, ttype, countries, **kw):
    terms = TournamentTerms.objects.create(
        slug=slug, version="1.0", title=slug, body="<p>x</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults = dict(
        name=slug, type=ttype, status=Tournament.Status.OPEN,
        start_time=timezone.now() + timedelta(days=1), capacity=16, rounds_total=5,
        is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="INR",
        allowed_countries=countries, terms=terms, terms_version="",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults), terms


class ResidencyFieldNameTest(TestCase):
    def test_israel_only_keeps_the_original_field(self):
        t, _ = _make("rf-il", Tournament.Type.GAUNTLET, ["IL"])
        self.assertEqual(residency_field_name(t), LEGACY_RESIDENCY)

    def test_other_countries_use_a_new_field(self):
        t, _ = _make("rf-in", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.assertEqual(residency_field_name(t), "confirmed_residency")


class TermsPageWordingTest(JoinTermsTestBase):
    def _page(self, tournament):
        return self.client.get(f"/tournaments/{tournament.pk}/terms/")

    def test_israel_only_wording_and_field_are_unchanged(self):
        t, _ = _make("pw-il", Tournament.Type.GAUNTLET, ["IL"])
        resp = self._page(t)
        self.assertContains(resp, f'name="{LEGACY_RESIDENCY}"')
        self.assertContains(resp, "I confirm that I am a <strong>resident of Israel</strong>.", html=False)
        self.assertContains(resp, f"<strong>{AGE_ISRAEL}</strong>")
        self.assertNotContains(resp, "age of majority")
        self.assertNotContains(resp, 'name="confirmed_residency"')

    def test_india_wording_and_field(self):
        self.set_country("IN")
        t, _ = _make("pw-in", Tournament.Type.GLADIATORMANIA, ["IN"])
        resp = self._page(t)
        self.assertContains(resp, 'name="confirmed_residency"')
        self.assertContains(resp, "I confirm that I am a <strong>resident of India</strong>.")
        self.assertContains(resp, f"<strong>{AGE_INDIA}</strong>")
        self.assertNotContains(resp, f'name="{LEGACY_RESIDENCY}"')
        self.assertNotContains(resp, "resident of Israel")
        self.assertNotContains(resp, AGE_ISRAEL)

    def test_several_countries_are_listed(self):
        self.set_country("IN")
        t, _ = _make("pw-multi", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self.assertContains(self._page(t), "<strong>resident of India and Israel</strong>")

    def test_context_values(self):
        t, _ = _make("pw-ctx", Tournament.Type.GLADIATORMANIA, ["IN"])
        ctx = build_terms_context(t)
        self.assertEqual((ctx["israel_only"], ctx["residency_countries"]), (False, "India"))

    def test_checkbox_hidden_when_terms_do_not_require_it(self):
        terms = TournamentTerms.objects.create(
            slug="pw-off", version="1.0", title="x", body="<p>x</p>",
            requires_age_18=False, requires_israeli_residency=False, has_prize=True,
        )
        t = Tournament.objects.create(
            name="pw-off", type=Tournament.Type.GAUNTLET, status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1), is_money_tournament=True,
            prize_amount=Decimal("5"), prize_currency="ILS", terms=terms,
        )
        resp = self._page(t)
        self.assertNotContains(resp, "cb_residency")
        self.assertNotContains(resp, "cb_age")


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
class JoinDeclarationTest(JoinTermsTestBase):
    """Server-side validation of the declaration and what gets stored."""

    BASE = {"terms_accepted": "1", "confirmed_age_18_plus": "1"}
    profile_country = "IN"   # the Israel-only tests below switch it to IL

    def _join(self, t, terms, country="IN", **post):
        data = {"terms_slug": terms.slug, "terms_version": terms.version, **post}
        with PASS_GATES[0], PASS_GATES[1], mock.patch(LOOKUP, return_value=country):
            return self.client.post(
                f"/tournaments/{t.pk}/join/", data=data, HTTP_X_FORWARDED_FOR="194.90.0.1",
            )

    def test_india_join_stores_declared_and_ip_country_side_by_side(self):
        t, terms = _make("jd-in", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.assertEqual(self._join(t, terms, **self.BASE, confirmed_residency="1").status_code, 302)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual(
            (row.declared_residency_countries, row.join_country_code, row.geo_eligible), ("IN", "IN", True),
        )
        self.assertTrue(row.confirmed_age_18_plus)
        self.assertFalse(row.confirmed_israeli_resident)  # not an Israeli declaration

    def test_crafted_post_without_the_new_checkbox_is_rejected(self):
        t, terms = _make("jd-craft", Tournament.Type.GLADIATORMANIA, ["IN"])
        for label, post in (
            ("omitted", dict(self.BASE)),
            ("old israeli field only", dict(self.BASE, **{LEGACY_RESIDENCY: "1"})),
            ("wrong value", dict(self.BASE, confirmed_residency="0")),
        ):
            with self.subTest(label):
                resp = self._join(t, terms, **post)
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/terms/", resp["Location"])
                self.assertFalse(self.joined(t))
                self.assertEqual(TournamentParticipant.objects.filter(tournament=t).count(), 0)

    def test_age_and_terms_are_still_required_for_india(self):
        t, terms = _make("jd-age", Tournament.Type.GLADIATORMANIA, ["IN"])
        self.assertRejected(self._join(t, terms, terms_accepted="1", confirmed_residency="1"), t)
        self.assertRejected(self._join(t, terms, confirmed_age_18_plus="1", confirmed_residency="1"), t)

    def test_several_countries_store_all_of_them(self):
        t, terms = _make("jd-multi", Tournament.Type.GLADIATORMANIA, ["IN", "IL"])
        self._join(t, terms, **self.BASE, confirmed_residency="1")
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual(row.declared_residency_countries, "IN,IL")

    def test_israel_only_join_is_unchanged(self):
        self.set_country("IL")
        t, terms = _make("jd-il", Tournament.Type.GAUNTLET, ["IL"])
        self.assertRejected(self._join(t, terms, "IL", **self.BASE, confirmed_residency="1"), t)  # new name is not accepted
        self.assertRejected(self._join(t, terms, "IL", **self.BASE), t)
        self.assertEqual(self._join(t, terms, "IL", **self.BASE, **{LEGACY_RESIDENCY: "1"}).status_code, 302)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertTrue(row.confirmed_israeli_resident)
        self.assertEqual((row.declared_residency_countries, row.join_country_code), ("IL", "IL"))

    def test_not_declared_stores_nothing(self):
        self.set_country("IL")
        terms = TournamentTerms.objects.create(
            slug="jd-off", version="1.0", title="x", body="<p>x</p>",
            requires_age_18=False, requires_israeli_residency=False, has_prize=True,
        )
        t = Tournament.objects.create(
            name="jd-off", type=Tournament.Type.GAUNTLET, status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1), capacity=16, rounds_total=5,
            is_money_tournament=True, prize_amount=Decimal("5"), prize_currency="ILS", terms=terms,
        )
        self._join(t, terms, "IL", terms_accepted="1")
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual(row.declared_residency_countries, "")
        self.assertFalse(row.confirmed_israeli_resident)


class TermsRequiredForOtherCountriesTest(TestCase):
    def _tournament(self, countries, **kw):
        defaults = dict(
            name="tr", type=Tournament.Type.GLADIATORMANIA, start_time=timezone.now() + timedelta(days=1),
            is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="INR",
            allowed_countries=countries,
        )
        defaults.update(kw)
        return Tournament(**defaults)

    def test_money_tournament_for_other_countries_needs_a_terms_record(self):
        with self.assertRaises(ValidationError) as ctx:
            self._tournament(["IN"]).clean()
        self.assertIn("needs a terms record", " ".join(ctx.exception.message_dict["terms"]))

    def test_israel_only_and_non_money_tournaments_do_not(self):
        self._tournament(["IL"]).clean()
        self._tournament(["IN"], is_money_tournament=False, prize_amount=None).clean()
