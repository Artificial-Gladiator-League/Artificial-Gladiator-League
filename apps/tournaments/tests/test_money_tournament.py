"""Tests for money tournament features:
- Model field defaults and properties
- IP geolocation helper (eligibility.py)
- Terms acceptance flow (money_tournament_terms view)
- Join gate: geo block, terms redirect, audit field storage
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone

from apps.tournaments.eligibility import (
    get_client_ip,
    lookup_country,
    check_geo_eligibility,
)
from apps.tournaments.models import Tournament, TournamentParticipant
from apps.users.models import UserGameModel

User = get_user_model()


# ── Helpers ──────────────────────────────────────────────────────────


def _user(username):
    return User.objects.create_user(
        username=username,
        email=f"{username}@test.com",
        password="testpass",
    )


def _money_tournament(**kw):
    defaults = dict(
        name="Money Tournament",
        type=Tournament.Type.GAUNTLET,
        game_type="chess",
        status=Tournament.Status.OPEN,
        start_time=timezone.now() + timedelta(days=1),
        capacity=16,
        rounds_total=5,
        current_round=0,
        is_money_tournament=True,
        prize_amount="500.00",
        prize_currency="ILS",
        terms_text="These are the terms.",
        terms_version="1.0",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


def _game_model(user, game_type="chess"):
    sha = "a" * 40
    return UserGameModel.objects.create(
        user=user,
        game_type=game_type,
        hf_model_repo_id=f"repo/{user.username}",
        approved_full_sha=sha,
        last_known_commit_id=sha,
        original_model_commit_sha=sha,
        submitted_ref="main",
        submission_repo_type="model",
        is_verified=True,
        model_integrity_ok=True,
        rated_games_played=30,
        rated_games_since_revalidation=30,
    )


# ── Model tests ───────────────────────────────────────────────────────


class TournamentMoneyFieldsTest(TestCase):

    def test_default_non_money_tournament(self):
        t = Tournament.objects.create(
            name="Free",
            type=Tournament.Type.GAUNTLET,
            game_type="chess",
            status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1),
        )
        self.assertFalse(t.is_money_tournament)
        self.assertIsNone(t.prize_amount)
        self.assertEqual(t.prize_currency, "ILS")
        self.assertEqual(t.payout_status, Tournament.PayoutStatus.PENDING)
        self.assertEqual(t.terms_text, "")
        self.assertEqual(t.terms_version, "")

    def test_money_tournament_fields_saved(self):
        t = _money_tournament()
        self.assertTrue(t.is_money_tournament)
        self.assertEqual(str(t.prize_amount), "500.00")
        self.assertEqual(t.prize_currency, "ILS")
        self.assertEqual(t.payout_status, Tournament.PayoutStatus.PENDING)
        self.assertEqual(t.terms_version, "1.0")

    def test_payout_status_choices(self):
        t = _money_tournament()
        t.payout_status = Tournament.PayoutStatus.PAID
        t.save(update_fields=["payout_status"])
        t.refresh_from_db()
        self.assertEqual(t.payout_status, "paid")


class ParticipantAuditFieldsTest(TestCase):

    def test_audit_fields_default_null(self):
        user = _user("alice")
        t = _money_tournament()
        p = TournamentParticipant.objects.create(tournament=t, user=user, seed=0)
        self.assertIsNone(p.join_ip)
        self.assertEqual(p.join_country_code, "")
        self.assertIsNone(p.geo_eligible)
        self.assertIsNone(p.terms_accepted_at)
        self.assertEqual(p.terms_version_accepted, "")

    def test_audit_fields_stored(self):
        user = _user("bob")
        t = _money_tournament()
        now = timezone.now()
        p = TournamentParticipant.objects.create(
            tournament=t, user=user, seed=0,
            join_ip="1.2.3.4",
            join_country_code="IL",
            geo_eligible=True,
            terms_accepted_at=now,
            terms_version_accepted="1.0",
        )
        p.refresh_from_db()
        self.assertEqual(p.join_ip, "1.2.3.4")
        self.assertEqual(p.join_country_code, "IL")
        self.assertTrue(p.geo_eligible)
        self.assertIsNotNone(p.terms_accepted_at)
        self.assertEqual(p.terms_version_accepted, "1.0")


# ── Eligibility module tests ──────────────────────────────────────────


class GetClientIPTest(TestCase):

    def _request(self, remote_addr="1.2.3.4", forwarded_for=None):
        factory = RequestFactory()
        req = factory.get("/")
        req.META["REMOTE_ADDR"] = remote_addr
        if forwarded_for:
            req.META["HTTP_X_FORWARDED_FOR"] = forwarded_for
        return req

    def test_remote_addr(self):
        req = self._request(remote_addr="5.6.7.8")
        self.assertEqual(get_client_ip(req), "5.6.7.8")

    def test_forwarded_for_single(self):
        req = self._request(forwarded_for="9.10.11.12")
        self.assertEqual(get_client_ip(req), "9.10.11.12")

    def test_forwarded_for_chain_takes_leftmost(self):
        req = self._request(forwarded_for="9.10.11.12, 172.16.0.1, 10.0.0.1")
        self.assertEqual(get_client_ip(req), "9.10.11.12")


@override_settings(
    MONEY_TOURNAMENT_GEO_ENABLED=True,
    MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES=["IL"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class LookupCountryTest(TestCase):

    def setUp(self):
        cache.clear()

    def test_successful_lookup(self):
        with mock.patch("apps.tournaments.eligibility.requests.get") as mock_get:
            mock_get.return_value = mock.Mock(
                status_code=200, text="IL", raise_for_status=lambda: None
            )
            result = lookup_country("1.2.3.4")
        self.assertEqual(result, "IL")

    def test_non_il_lookup(self):
        with mock.patch("apps.tournaments.eligibility.requests.get") as mock_get:
            mock_get.return_value = mock.Mock(
                status_code=200, text="US", raise_for_status=lambda: None
            )
            result = lookup_country("8.8.8.8")
        self.assertEqual(result, "US")

    def test_network_failure_returns_none(self):
        import requests as req_lib
        with mock.patch(
            "apps.tournaments.eligibility.requests.get",
            side_effect=req_lib.RequestException("timeout"),
        ):
            result = lookup_country("1.2.3.4")
        self.assertIsNone(result)

    def test_invalid_response_returns_none(self):
        with mock.patch("apps.tournaments.eligibility.requests.get") as mock_get:
            mock_get.return_value = mock.Mock(
                status_code=200, text="NOTACODE", raise_for_status=lambda: None
            )
            result = lookup_country("1.2.3.4")
        self.assertIsNone(result)


@override_settings(
    MONEY_TOURNAMENT_GEO_ENABLED=True,
    MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES=["IL"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class CheckGeoEligibilityTest(TestCase):

    def setUp(self):
        cache.clear()

    def _request(self, remote_addr="1.2.3.4"):
        factory = RequestFactory()
        req = factory.get("/")
        req.META["REMOTE_ADDR"] = remote_addr
        return req

    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False)
    def test_geo_disabled_returns_none_eligible(self):
        req = self._request()
        ip, country, eligible = check_geo_eligibility(req)
        self.assertIsNone(eligible)

    def test_private_ip_skips_check(self):
        req = self._request(remote_addr="127.0.0.1")
        ip, country, eligible = check_geo_eligibility(req)
        self.assertEqual(ip, "127.0.0.1")
        self.assertIsNone(eligible)

    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
    def test_il_ip_eligible(self):
        with mock.patch("apps.tournaments.eligibility.lookup_country", return_value="IL"):
            req = self._request(remote_addr="194.90.0.1")
            ip, country, eligible = check_geo_eligibility(req)
        self.assertEqual(country, "IL")
        self.assertTrue(eligible)

    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
    def test_non_il_ip_ineligible(self):
        with mock.patch("apps.tournaments.eligibility.lookup_country", return_value="US"):
            req = self._request(remote_addr="8.8.8.8")
            ip, country, eligible = check_geo_eligibility(req)
        self.assertEqual(country, "US")
        self.assertFalse(eligible)

    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
    def test_lookup_failure_fails_closed(self):
        with mock.patch("apps.tournaments.eligibility.lookup_country", return_value=None):
            req = self._request(remote_addr="1.2.3.4")
            ip, country, eligible = check_geo_eligibility(req)
        self.assertFalse(eligible)


# ── View-level integration tests ──────────────────────────────────────


@override_settings(
    MONEY_TOURNAMENT_GEO_ENABLED=False,   # bypass geo for view tests
    HF_PLATFORM_TOKEN="hf_test",
)
class MoneyTermsViewTest(TestCase):

    def setUp(self):
        self.user = _user("carol")
        self.client.force_login(self.user)
        self.tournament = _money_tournament()

    def test_get_terms_page_with_paypal_email(self):
        """GET terms page renders when user has a PayPal email on their profile."""
        self.user.paypal_email = "carol@paypal.com"
        self.user.save()
        resp = self.client.get(
            f"/tournaments/{self.tournament.pk}/terms/", follow=False
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Terms")

    def test_get_terms_page_redirects_when_no_paypal_email(self):
        """GET terms redirects to profile PayPal tab when no profile email."""
        # user has no paypal_email (default '')
        resp = self.client.get(
            f"/tournaments/{self.tournament.pk}/terms/", follow=False
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/profile/", resp["Location"])
        self.assertIn("paypal", resp["Location"])

    def test_post_without_checkbox_shows_error(self):
        self.user.paypal_email = "carol@paypal.com"
        self.user.save()
        resp = self.client.post(
            f"/tournaments/{self.tournament.pk}/terms/",
            data={},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "tick the checkbox")

    def test_post_with_checkbox_redirects_to_detail(self):
        self.user.paypal_email = "carol@paypal.com"
        self.user.save()
        resp = self.client.post(
            f"/tournaments/{self.tournament.pk}/terms/",
            data={"terms_accepted": "1"},
            follow=False,
        )
        # Terms view now just redirects to detail; the real join
        # happens when the form POSTs directly to /join/.
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/tournaments/{self.tournament.pk}/", resp["Location"])

    def test_non_money_tournament_404(self):
        free_t = Tournament.objects.create(
            name="Free",
            type=Tournament.Type.GAUNTLET,
            game_type="chess",
            status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1),
        )
        resp = self.client.get(f"/tournaments/{free_t.pk}/terms/")
        self.assertEqual(resp.status_code, 404)


@override_settings(
    MONEY_TOURNAMENT_GEO_ENABLED=True,
    MONEY_TOURNAMENT_ELIGIBLE_COUNTRIES=["IL"],
    HF_PLATFORM_TOKEN="hf_test",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class JoinMoneyTournamentGeoTest(TestCase):
    """Verify that the geo gate in join_tournament blocks non-IL IPs."""

    def setUp(self):
        self.user = _user("dana")
        self.client.force_login(self.user)
        self.tournament = _money_tournament(terms_version="")  # skip terms gate
        _game_model(self.user)

    def _join(self, ip="1.2.3.4"):
        return self.client.post(
            f"/tournaments/{self.tournament.pk}/join/",
            HTTP_X_FORWARDED_FOR=ip,
            follow=False,
        )

    def test_non_il_ip_blocked(self):
        with mock.patch(
            "apps.tournaments.eligibility.lookup_country", return_value="US"
        ):
            resp = self._join(ip="8.8.8.8")
        # Should redirect back to detail, not join
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(
            self.user,
            self.tournament.players.all(),
        )

    def test_il_ip_allowed(self):
        # Set PayPal email on the user's profile so the new gate passes.
        self.user.paypal_email = "dana@paypal.com"
        self.user.save()
        with (
            mock.patch(
                "apps.tournaments.eligibility.lookup_country", return_value="IL"
            ),
            mock.patch("apps.users.integrity.live_sha_check", return_value=(True, "a" * 40, "a" * 40)),
            mock.patch("apps.users.integrity.can_join_tournament", return_value=(True, "")),
        ):
            resp = self._join(ip="194.90.0.1")
        # Should be registered (redirect to detail)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(self.user, self.tournament.players.all())


# ── PayPal email tests ────────────────────────────────────────────────


@override_settings(
    MONEY_TOURNAMENT_GEO_ENABLED=False,
    HF_PLATFORM_TOKEN="hf_test",
)
class PayPalEmailRegistrationTest(TestCase):
    """Verify that paypal_email is stored on the participant at registration."""

    def setUp(self):
        self.user = _user("eve")
        self.client.force_login(self.user)
        # No terms_version so we only need the profile paypal_email.
        self.tournament = _money_tournament(terms_version="")
        _game_model(self.user)

    def test_paypal_email_stored_at_registration(self):
        """PayPal email from user profile is saved on the participant record."""
        self.user.paypal_email = "eve@paypal.com"
        self.user.save()
        with (
            mock.patch("apps.users.integrity.live_sha_check", return_value=(True, "a" * 40, "a" * 40)),
            mock.patch("apps.users.integrity.can_join_tournament", return_value=(True, "")),
        ):
            resp = self.client.post(
                f"/tournaments/{self.tournament.pk}/join/", follow=False
            )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(self.user, self.tournament.players.all())
        p = TournamentParticipant.objects.get(tournament=self.tournament, user=self.user)
        self.assertEqual(p.paypal_email, "eve@paypal.com")

    def test_join_redirects_to_terms_page_when_paypal_email_missing(self):
        """Without a PayPal email on the profile the join is redirected to terms."""
        # user.paypal_email is '' by default
        with (
            mock.patch("apps.users.integrity.live_sha_check", return_value=(True, "a" * 40, "a" * 40)),
            mock.patch("apps.users.integrity.can_join_tournament", return_value=(True, "")),
        ):
            resp = self.client.post(
                f"/tournaments/{self.tournament.pk}/join/", follow=False
            )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/terms/", resp["Location"])
        self.assertNotIn(self.user, self.tournament.players.all())

    def test_paypal_email_default_is_empty_for_non_money_participant(self):
        """A participant in a free tournament has an empty paypal_email by default."""
        free = Tournament.objects.create(
            name="Free",
            type=Tournament.Type.GAUNTLET,
            game_type="chess",
            status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1),
        )
        p = TournamentParticipant.objects.create(tournament=free, user=self.user, seed=0)
        self.assertEqual(p.paypal_email, "")


class PayPalEmailCleanupTest(TestCase):
    """Verify that _complete_tournament clears non-winner PayPal emails."""

    # Use the engine function directly — no HTTP involved.

    def _make_tournament_with_participants(self, n=3):
        """Return a money tournament with *n* participants, each with a PayPal email."""
        t = _money_tournament()
        users = []
        for i in range(n):
            u = _user(f"player{i}")
            p = TournamentParticipant.objects.create(
                tournament=t,
                user=u,
                seed=i,
                paypal_email=f"player{i}@paypal.com",
            )
            users.append((u, p))
        return t, users

    def _complete(self, tournament, winner_user):
        """Set the winner as the sole non-eliminated participant, then call _complete_tournament."""
        from apps.tournaments.engine import _complete_tournament
        # Mark everyone else as eliminated so _complete_tournament picks the right champion.
        TournamentParticipant.objects.filter(
            tournament=tournament,
        ).exclude(user=winner_user).update(eliminated=True)
        _complete_tournament(tournament)
        tournament.refresh_from_db()

    def test_non_winners_paypal_email_cleared_at_tournament_end(self):
        """All participants except the winner have paypal_email cleared automatically."""
        t, users = self._make_tournament_with_participants(n=3)
        winner_user, _ = users[0]
        self._complete(t, winner_user)

        for u, _ in users[1:]:
            p = TournamentParticipant.objects.get(tournament=t, user=u)
            self.assertEqual(
                p.paypal_email, "",
                msg=f"Expected empty paypal_email for non-winner {u.username}",
            )

    def test_winner_paypal_email_retained_at_tournament_end(self):
        """The champion's paypal_email is NOT cleared — admin must do it manually."""
        t, users = self._make_tournament_with_participants(n=3)
        winner_user, _ = users[0]
        self._complete(t, winner_user)

        p = TournamentParticipant.objects.get(tournament=t, user=winner_user)
        self.assertEqual(
            p.paypal_email, "player0@paypal.com",
            msg="Winner's paypal_email must be preserved until admin manually clears it.",
        )

    def test_no_unrelated_fields_changed_by_cleanup(self):
        """paypal_email cleanup does not touch other participant fields."""
        t, users = self._make_tournament_with_participants(n=2)
        winner_user, winner_p = users[0]
        non_winner_user, non_winner_p = users[1]
        # Capture current state of an unrelated field.
        original_seed = non_winner_p.seed
        self._complete(t, winner_user)

        non_winner_p.refresh_from_db()
        self.assertEqual(non_winner_p.seed, original_seed)
        # eliminated flag is set by the test helper (update), not by _complete_tournament.
        # Confirm paypal_email is the only changed field.
        self.assertEqual(non_winner_p.paypal_email, "")

    def test_no_paypal_cleanup_for_non_money_tournament(self):
        """paypal_email is not touched when the tournament is not a cash tournament."""
        from apps.tournaments.engine import _complete_tournament
        free = Tournament.objects.create(
            name="Free",
            type=Tournament.Type.GAUNTLET,
            game_type="chess",
            status=Tournament.Status.ONGOING,
            start_time=timezone.now() - timedelta(hours=1),
        )
        u = _user("freeuser")
        p = TournamentParticipant.objects.create(
            tournament=free, user=u, seed=0,
            paypal_email="freeuser@paypal.com",
        )
        _complete_tournament(free)
        p.refresh_from_db()
        # Free tournament: paypal_email must remain unchanged.
        self.assertEqual(p.paypal_email, "freeuser@paypal.com")

    def test_all_paypal_emails_cleared_when_no_champion(self):
        """If there is no surviving participant (edge case), everyone's email is cleared."""
        from apps.tournaments.engine import _complete_tournament
        t = _money_tournament()
        u1 = _user("ghost1")
        u2 = _user("ghost2")
        for i, u in enumerate([u1, u2]):
            TournamentParticipant.objects.create(
                tournament=t, user=u, seed=i,
                paypal_email=f"ghost{i}@paypal.com",
                eliminated=True,
            )
        _complete_tournament(t)
        for u in [u1, u2]:
            p = TournamentParticipant.objects.get(tournament=t, user=u)
            self.assertEqual(p.paypal_email, "")
