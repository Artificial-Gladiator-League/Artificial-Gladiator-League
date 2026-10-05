from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.tournaments.countries import countries_phrase, country_name
from apps.tournaments.eligibility import (
    ISRAEL_ONLY_BLOCK_MESSAGE, UNVERIFIED_LOCATION_MESSAGE, check_geo_eligibility, geo_block_message,
)
from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.tests.test_terms_join import JoinTermsTestBase

LOOKUP = "apps.tournaments.eligibility.lookup_country"
PUBLIC_IP = "194.90.0.1"
OLD_ISRAEL_MESSAGE = (
    "This tournament is open to Israeli residents only. "
    "Your location does not qualify. "
    "If you believe this is an error, please contact support."
)
UNDETERMINED = "We could not verify your location. Please try again later or contact us."


def _request(ip=PUBLIC_IP):
    return RequestFactory().get("/", HTTP_X_FORWARDED_FOR=ip)


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
class CheckGeoEligibilityTest(SimpleTestCase):
    def check(self, country, allowed):
        with mock.patch(LOOKUP, return_value=country):
            return check_geo_eligibility(_request(), allowed)

    def test_israel_only_is_unchanged(self):
        self.assertEqual(self.check("IL", ["IL"]), (PUBLIC_IP, "IL", True))
        self.assertEqual(self.check("US", ["IL"]), (PUBLIC_IP, "US", False))
        self.assertEqual(self.check("IN", ["IL"]), (PUBLIC_IP, "IN", False))

    def test_uses_the_tournaments_countries(self):
        self.assertEqual(self.check("IN", ["IN"]), (PUBLIC_IP, "IN", True))
        self.assertEqual(self.check("IL", ["IN"]), (PUBLIC_IP, "IL", False))
        self.assertEqual(self.check("IL", ["IN", "il"]), (PUBLIC_IP, "IL", True))

    def test_undetermined_country_fails_closed_for_every_list(self):
        for allowed in (["IL"], ["IN"]):
            self.assertEqual(self.check(None, allowed), (PUBLIC_IP, None, False))

    def test_empty_list_means_open_to_all(self):
        with mock.patch(LOOKUP) as lookup:
            self.assertEqual(check_geo_eligibility(_request(), []), (PUBLIC_IP, None, None))
        lookup.assert_not_called()

    @override_settings(MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES=["US"])
    def test_global_list_is_only_a_fallback_when_no_list_is_passed(self):
        with mock.patch(LOOKUP, return_value="US"):
            self.assertEqual(check_geo_eligibility(_request()), (PUBLIC_IP, "US", True))
            self.assertEqual(check_geo_eligibility(_request(), ["IL"]), (PUBLIC_IP, "US", False))

    def test_private_ip_and_disabled_switch_skip_the_check(self):
        with mock.patch(LOOKUP) as lookup:
            self.assertEqual(check_geo_eligibility(_request("10.0.0.5"), ["IN"]), ("10.0.0.5", None, None))
            with override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False):
                self.assertEqual(check_geo_eligibility(_request(), ["IN"]), (PUBLIC_IP, None, None))
            lookup.assert_not_called()


class BlockMessageTest(SimpleTestCase):
    def test_israel_only_messages_are_the_original_text(self):
        self.assertEqual(ISRAEL_ONLY_BLOCK_MESSAGE, OLD_ISRAEL_MESSAGE)
        self.assertEqual(geo_block_message(["IL"], "US"), OLD_ISRAEL_MESSAGE)
        self.assertEqual(geo_block_message(["IL"], None), OLD_ISRAEL_MESSAGE)

    def test_other_countries(self):
        self.assertEqual(UNVERIFIED_LOCATION_MESSAGE, UNDETERMINED)
        self.assertEqual(geo_block_message(["IN"], None), UNDETERMINED)
        self.assertEqual(
            geo_block_message(["IN"], "US"),
            "This tournament is open to residents of India only. "
            "Your location does not qualify. If you believe this is an error, please contact support.",
        )

    def test_country_names(self):
        self.assertEqual(country_name("IN"), "India")
        self.assertEqual(country_name("ZZ"), "ZZ")
        self.assertEqual(countries_phrase(["IN"]), "India")
        self.assertEqual(countries_phrase(["IN", "IL"]), "India and Israel")
        self.assertEqual(countries_phrase(["IN", "IL", "US"]), "India, Israel and United States")


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True, MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES=["US"])
class JoinGeoGateTest(JoinTermsTestBase):
    """The join view applies the tournament's own countries, never the global list."""

    # Israel-only forms carry confirmed_israeli_resident, other countries confirmed_residency.
    ALL = {
        "terms_accepted": "1", "confirmed_age_18_plus": "1",
        "confirmed_israeli_resident": "1", "confirmed_residency": "1",
    }

    def _setup(self, ttype, countries, slug):
        terms = TournamentTerms.objects.create(
            slug=slug, version="1.0", title=slug, body="<p>x</p>",
            requires_age_18=True, requires_israeli_residency=True, has_prize=True,
        )
        t = Tournament.objects.create(
            name=slug, type=ttype, status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1), capacity=16, rounds_total=5,
            is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="INR",
            allowed_countries=countries, terms=terms, terms_version="",
        )
        return t, terms

    def _join_from(self, t, terms, country):
        """POST the join form; *country* is what the IP lookup returns."""
        from apps.tournaments.tests.test_terms_join import PASS_GATES
        with PASS_GATES[0], PASS_GATES[1], mock.patch(LOOKUP, return_value=country):
            return self.client.post(
                f"/tournaments/{t.pk}/join/", data=self.shown(terms, **self.ALL),
                HTTP_X_FORWARDED_FOR=PUBLIC_IP,
            )

    def _message(self, resp):
        # Unrendered messages pile up across posts, so only the newest one is meaningful.
        return [str(m) for m in get_messages(resp.wsgi_request)][-1:]

    def test_israel_only_gauntlet_is_identical(self):
        t, terms = self._setup(Tournament.Type.GAUNTLET, ["IL"], "g-il")
        resp = self._join_from(t, terms, "US")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/tournaments/{t.pk}/", resp["Location"])
        self.assertFalse(self.joined(t))
        self.assertEqual(self._message(resp), [OLD_ISRAEL_MESSAGE])

        resp = self._join_from(t, terms, None)  # lookup failure: same fail-closed message as before
        self.assertFalse(self.joined(t))
        self.assertEqual(self._message(resp), [OLD_ISRAEL_MESSAGE])

        self.assertEqual(self._join_from(t, terms, "IL").status_code, 302)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual((row.join_country_code, row.geo_eligible, row.join_ip), ("IL", True, PUBLIC_IP))

    def test_india_tournament_allows_only_india(self):
        self.set_country("IN")
        t, terms = self._setup(Tournament.Type.GLADIATORMANIA, ["IN"], "m-in")
        for country in ("IL", "US"):
            resp = self._join_from(t, terms, country)
            self.assertFalse(self.joined(t), country)
            self.assertEqual(self._message(resp), [
                "This tournament is open to residents of India only. "
                "Your location does not qualify. If you believe this is an error, please contact support."
            ])

        self.assertEqual(self._join_from(t, terms, "IN").status_code, 302)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual((row.join_country_code, row.geo_eligible), ("IN", True))

    def test_india_undetermined_country_uses_the_new_message_and_stores_nothing(self):
        self.set_country("IN")
        t, terms = self._setup(Tournament.Type.GLADIATORMANIA, ["IN"], "m-in2")
        resp = self._join_from(t, terms, None)
        self.assertEqual(self._message(resp), [UNDETERMINED])
        self.assertFalse(self.joined(t))
        self.assertEqual(TournamentParticipant.objects.filter(tournament=t).count(), 0)

    def test_israel_only_gladiatormania_is_unchanged_too(self):
        t, terms = self._setup(Tournament.Type.GLADIATORMANIA, ["IL"], "m-il")
        self.assertEqual(self._message(self._join_from(t, terms, "IN")), [OLD_ISRAEL_MESSAGE])
        self.assertFalse(self.joined(t))
        self.assertEqual(self._join_from(t, terms, "IL").status_code, 302)
        self.assertTrue(self.joined(t))

    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False)
    def test_master_switch_still_bypasses_the_gate(self):
        self.set_country("IN")
        t, terms = self._setup(Tournament.Type.GLADIATORMANIA, ["IN"], "m-off")
        with mock.patch(LOOKUP) as lookup:
            from apps.tournaments.tests.test_terms_join import PASS_GATES
            with PASS_GATES[0], PASS_GATES[1]:
                self.client.post(
                    f"/tournaments/{t.pk}/join/", data=self.shown(terms, **self.ALL),
                    HTTP_X_FORWARDED_FOR=PUBLIC_IP,
                )
            lookup.assert_not_called()
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual((row.join_country_code, row.geo_eligible), ("", None))
