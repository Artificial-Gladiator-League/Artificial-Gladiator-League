from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.tests.test_money_tournament import _game_model

User = get_user_model()

PASS_GATES = (
    mock.patch("apps.users.integrity.live_sha_check", return_value=(True, "a" * 40, "a" * 40)),
    mock.patch("apps.users.integrity.can_join_tournament", return_value=(True, "")),
)


def _terms(slug="join-terms", version="3.1", **kw):
    defaults = dict(
        slug=slug, title="Join Terms", version=version,
        body="<p>{{ tournament.prize_amount }} {{ tournament.prize_currency }}</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(**defaults)


def _tournament(**kw):
    defaults = dict(
        name="Join Money", type=Tournament.Type.GAUNTLET, status=Tournament.Status.OPEN,
        start_time=timezone.now() + timedelta(days=1), capacity=16, rounds_total=5,
        is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="ILS",
        terms_version="",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False, HF_PLATFORM_TOKEN="hf_test")
class JoinTermsTestBase(TestCase):
    paypal = "pal@example.com"
    profile_country = ""   # set to a code to give the player that profile country (country-restricted tests)

    def set_country(self, code):
        """Test fixture only: write the profile country directly (the lock guards the normal paths)."""
        User.objects.filter(pk=self.user.pk).update(country=code, country_locked=bool(code))
        self.user.refresh_from_db()

    def setUp(self):
        self.user = User.objects.create_user("joiner", password="pw")
        self.user.paypal_email = self.paypal
        self.user.save(update_fields=["paypal_email"])
        if self.profile_country:
            self.set_country(self.profile_country)
        self.client.force_login(self.user)
        _game_model(self.user)

    def post_join(self, tournament, data=None, follow=False):
        with PASS_GATES[0], PASS_GATES[1]:
            return self.client.post(
                reverse("tournaments:join", args=[tournament.pk]), data=data or {}, follow=follow,
            )

    @staticmethod
    def shown(terms, **extra):
        """The hidden fields the terms page renders, plus whatever the player ticked."""
        data = {"terms_slug": terms.slug, "terms_version": terms.version}
        data.update(extra)
        return data

    def joined(self, tournament):
        return TournamentParticipant.objects.filter(tournament=tournament, user=self.user).exists()

    def assertRejected(self, resp, tournament):
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/terms/", resp["Location"])
        self.assertFalse(self.joined(tournament))


class RequiredConfirmationsFollowFlagsTest(JoinTermsTestBase):
    AGE = {"confirmed_age_18_plus": "1"}
    RES = {"confirmed_israeli_resident": "1"}
    TERMS = {"terms_accepted": "1"}

    def test_server_enforces_exactly_the_flagged_confirmations(self):
        cases = [
            # (requires_age, requires_residency, ticked, joins)
            (True, True, {**self.AGE, **self.RES, **self.TERMS}, True),
            (True, True, {**self.RES, **self.TERMS}, False),   # age omitted
            (True, True, {**self.AGE, **self.TERMS}, False),   # residency omitted
            (True, True, {**self.AGE, **self.RES}, False),     # terms box omitted
            (True, True, {}, False),
            (False, False, {**self.TERMS}, True),              # crafted POST: only what is required
            (False, False, {}, False),                         # the terms box is always required
            (True, False, {**self.AGE, **self.TERMS}, True),
            (True, False, {**self.TERMS}, False),
            (False, True, {**self.RES, **self.TERMS}, True),
            (False, True, {**self.TERMS}, False),
        ]
        for i, (age, res, ticked, joins) in enumerate(cases):
            with self.subTest(age=age, res=res, ticked=sorted(ticked)):
                terms = _terms(slug=f"flags-{i}", requires_age_18=age, requires_israeli_residency=res)
                t = _tournament(name=f"T{i}", terms=terms)
                resp = self.post_join(t, self.shown(terms, **ticked))
                if joins:
                    self.assertEqual(resp.status_code, 302)
                    self.assertTrue(self.joined(t))
                else:
                    self.assertRejected(resp, t)

    def test_unrequired_declarations_are_stored_as_not_declared(self):
        terms = _terms(requires_age_18=False, requires_israeli_residency=False)
        t = _tournament(terms=terms)
        self.post_join(t, self.shown(terms, **self.TERMS))
        p = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertFalse(p.confirmed_age_18_plus)
        self.assertFalse(p.confirmed_israeli_resident)


class PayPalRuleFollowsHasPrizeTest(JoinTermsTestBase):
    paypal = ""

    def _data(self, terms):
        return self.shown(terms, terms_accepted="1", confirmed_age_18_plus="1", confirmed_israeli_resident="1")

    def test_join_without_paypal_email(self):
        no_prize = _terms(slug="no-prize", has_prize=False)
        t1 = _tournament(name="NoPrize", terms=no_prize)
        self.post_join(t1, self._data(no_prize))
        self.assertTrue(self.joined(t1))

        prize = _terms(slug="prize", has_prize=True)
        t2 = _tournament(name="Prize", terms=prize)
        self.assertRejected(self.post_join(t2, self._data(prize)), t2)

    def test_no_terms_still_requires_paypal_email(self):
        t = _tournament(terms_version="1.0")
        data = {"terms_accepted": "1", "confirmed_age_18_plus": "1", "confirmed_israeli_resident": "1"}
        self.assertRejected(self.post_join(t, data), t)

    def test_terms_page_uses_the_same_rule(self):
        no_prize = _tournament(name="NP", terms=_terms(slug="np", has_prize=False))
        resp = self.client.get(reverse("tournaments:money_terms", args=[no_prize.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Payout address on file")

        prize = _tournament(name="P", terms=_terms(slug="p", has_prize=True))
        resp = self.client.get(reverse("tournaments:money_terms", args=[prize.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=paypal", resp["Location"])

        legacy = _tournament(name="L", terms_version="1.0")
        resp = self.client.get(reverse("tournaments:money_terms", args=[legacy.pk]))
        self.assertEqual(resp.status_code, 302)


class TermsVersionMismatchTest(JoinTermsTestBase):
    ALL = {"terms_accepted": "1", "confirmed_age_18_plus": "1", "confirmed_israeli_resident": "1"}

    def test_stale_version_is_rejected_and_nothing_is_stored(self):
        v1 = _terms(version="1.0")
        t = _tournament(terms=v1)
        shown = self.shown(v1, **self.ALL)
        t.terms = _terms(version="2.0")  # the admin publishes a new version meanwhile
        t.save()

        resp = self.post_join(t, shown, follow=True)
        self.assertContains(resp, "changed since you opened the page")
        self.assertFalse(self.joined(t))
        self.assertEqual(t.players.count(), 0)

    def test_wrong_or_missing_slug_and_version_are_rejected(self):
        terms = _terms()
        t = _tournament(terms=terms)
        for bad in (
            {**self.ALL},
            {**self.ALL, "terms_slug": terms.slug},
            {**self.ALL, "terms_version": terms.version},
            {**self.ALL, "terms_slug": "other", "terms_version": terms.version},
            {**self.ALL, "terms_slug": terms.slug, "terms_version": "9.9"},
        ):
            with self.subTest(bad=bad):
                self.assertRejected(self.post_join(t, bad), t)

    def test_terms_attached_after_the_page_was_loaded_is_rejected(self):
        t = _tournament(terms_version="1.0")
        legacy_form = dict(self.ALL)  # no hidden fields: the page had no terms record
        t.terms = _terms()
        t.save()
        self.assertRejected(self.post_join(t, legacy_form), t)

    def test_terms_page_renders_the_hidden_fields(self):
        terms = _terms()
        t = _tournament(terms=terms)
        resp = self.client.get(reverse("tournaments:money_terms", args=[t.pk]))
        self.assertContains(resp, f'name="terms_slug" value="{terms.slug}"')
        self.assertContains(resp, f'name="terms_version" value="{terms.version}"')


class AcceptedVersionStoredTest(JoinTermsTestBase):
    def test_terms_slug_and_version_are_stored(self):
        terms = _terms(slug="stored", version="4.2")
        t = _tournament(terms=terms, terms_version="legacy-ignored")
        resp = self.post_join(t, self.shown(
            terms, terms_accepted="1", confirmed_age_18_plus="1", confirmed_israeli_resident="1",
        ))
        self.assertEqual(resp.status_code, 302)
        p = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual((p.accepted_terms_slug, p.terms_version_accepted), ("stored", "4.2"))
        self.assertIsNotNone(p.terms_accepted_at)
        self.assertTrue(p.confirmed_age_18_plus and p.confirmed_israeli_resident)
        self.assertEqual(terms.acceptance_count(), 1)


class NoTermsBehaviourUnchangedTest(JoinTermsTestBase):
    ALL = {"terms_accepted": "1", "confirmed_age_18_plus": "1", "confirmed_israeli_resident": "1"}

    def test_all_three_required_and_legacy_version_stored(self):
        t = _tournament(terms_version="1.0")
        self.assertIsNone(t.terms)
        self.assertRejected(self.post_join(t, {k: v for k, v in self.ALL.items() if k != "confirmed_age_18_plus"}), t)
        self.assertRejected(self.post_join(t, {k: v for k, v in self.ALL.items() if k != "confirmed_israeli_resident"}), t)
        self.assertRejected(self.post_join(t, {k: v for k, v in self.ALL.items() if k != "terms_accepted"}), t)

        self.assertEqual(self.post_join(t, self.ALL).status_code, 302)
        p = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual((p.terms_version_accepted, p.accepted_terms_slug), ("1.0", ""))

    def test_terms_box_only_required_when_legacy_version_is_set(self):
        t = _tournament(terms_version="")
        data = {"confirmed_age_18_plus": "1", "confirmed_israeli_resident": "1"}
        self.assertEqual(self.post_join(t, data).status_code, 302)
        self.assertTrue(self.joined(t))

    def test_stray_terms_fields_are_rejected_when_there_are_no_terms(self):
        t = _tournament(terms_version="1.0")
        self.assertRejected(self.post_join(t, {**self.ALL, "terms_slug": "x", "terms_version": "1.0"}), t)


class ConfirmationEmailSeesAcceptedTermsTest(JoinTermsTestBase):
    def test_acceptance_is_stored_before_the_confirmation_email_is_built(self):
        terms = _terms(slug="mail", version="7.0")
        t = _tournament(terms=terms)
        seen = {}

        def fake_send(tournament_id, user_id, seconds):
            seen["stored"] = TournamentParticipant.objects.values_list(
                "accepted_terms_slug", "terms_version_accepted",
            ).get(tournament_id=tournament_id, user_id=user_id)

        with mock.patch("apps.tournaments.tasks.send_registration_confirmation", side_effect=fake_send):
            self.post_join(t, self.shown(
                terms, terms_accepted="1", confirmed_age_18_plus="1", confirmed_israeli_resident="1",
            ))
        self.assertEqual(seen["stored"], ("mail", "7.0"))
