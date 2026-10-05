import re
from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.games.models import Game
from apps.tournaments.admin import TournamentAdmin
from apps.tournaments.models import (
    EligibilityVerification, Match, PrizeClaim, Tournament, TournamentParticipant, TournamentTerms,
)
from apps.tournaments.tests.test_money_tournament import _game_model

User = get_user_model()
ADMIN_JS = Path(__file__).resolve().parents[3] / "static" / "admin" / "js" / "tournament_type_fields.js"
CONFIRM = {"terms_accepted": "1", "confirmed_age_18_plus": "1", "confirmed_israeli_resident": "1"}


def _terms(slug, version="1.0", **kw):
    defaults = dict(
        title=f"Terms {slug}", body="<p>{{ tournament.prize_amount }}</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(slug=slug, version=version, **defaults)


def _tournament(ttype, name, **kw):
    defaults = dict(
        name=name, type=ttype, status=Tournament.Status.OPEN, game_type="chess",
        start_time=timezone.now() + timedelta(days=1), capacity=2, rounds_total=1,
        is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="ILS",
        terms_version="",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


def _external_patches(stack, *, confirmation_email=False):
    """Everything that would call Hugging Face, spawn bot threads or hit the channel layer."""
    targets = {
        "apps.users.integrity.live_sha_check": dict(return_value=(True, "a" * 40, "a" * 40)),
        "apps.users.integrity.can_join_tournament": dict(return_value=(True, "")),
        "apps.tournaments.engine.pre_round_sha_check": dict(return_value=[]),
        "apps.tournaments.engine._start_bot_game_thread": {},
        "apps.tournaments.sha_audit.capture_round_baseline": dict(return_value=0),
        "apps.tournaments.sha_audit.schedule_round_integrity_check": dict(return_value=0.0),
        "apps.tournaments.sha_audit.schedule_immediate_start_check": {},
        "apps.tournaments.tasks.run_registration_period_sha_audit": {},
        "apps.users.signals._broadcast_leaderboard_refresh": {},
    }
    if not confirmation_email:
        targets["apps.tournaments.tasks.send_registration_confirmation"] = {}
    for target, kwargs in targets.items():
        stack.enter_context(mock.patch(target, **kwargs))


def _players(count=2, email=False):
    users = []
    for i in range(count):
        user = User.objects.create_user(f"player{i}", password="pw", email=f"player{i}@example.com" if email else "")
        user.paypal_email = f"player{i}@paypal.test"
        user.save(update_fields=["paypal_email"])
        _game_model(user)
        users.append(user)
    return users


def _join(user, tournament, terms):
    client = Client()
    client.force_login(user)
    data = {"terms_slug": terms.slug, "terms_version": terms.version, **CONFIRM}
    return client.post(reverse("tournaments:join", args=[tournament.pk]), data=data)


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False, HF_PLATFORM_TOKEN="hf_test")
class MoneyLikeLifecycleTest(TestCase):
    """Gauntlet and Gladiatormania must walk exactly the same registration -> completion path."""

    def test_same_lifecycle_for_every_money_like_type(self):
        self.assertEqual(
            set(Tournament.MONEY_LIKE_TYPES),
            {Tournament.Type.GAUNTLET, Tournament.Type.GLADIATORMANIA},
        )
        players = _players()
        for ttype in Tournament.MONEY_LIKE_TYPES:
            with self.subTest(type=ttype), ExitStack() as stack:
                _external_patches(stack)
                terms = _terms(f"life-{ttype}")
                t = _tournament(ttype, f"Lifecycle {ttype}", terms=terms)

                # Registration: the second join fills the bracket and starts it.
                for user in players:
                    self.assertEqual(_join(user, t, terms).status_code, 302)
                t.refresh_from_db()
                self.assertEqual((t.status, t.current_round), (Tournament.Status.ONGOING, 1))
                self.assertEqual(
                    set(TournamentParticipant.objects.filter(tournament=t).values_list(
                        "accepted_terms_slug", "terms_version_accepted")),
                    {(terms.slug, "1.0")},
                )
                match = Match.objects.get(tournament=t, is_armageddon=False)
                self.assertEqual(match.match_status, Match.MatchStatus.LIVE)
                game = Game.objects.get(tournament_match=match)

                # Completion: a decisive result runs the post_save chain into the engine.
                game.status = Game.Status.WHITE_WINS
                game.result = Game.Result.WHITE_WIN
                game.winner = game.white
                game.result_reason = "checkmate"
                game.save()

                t.refresh_from_db()
                self.assertEqual(t.status, Tournament.Status.COMPLETED)
                self.assertEqual(t.champion, game.white)

                verification = EligibilityVerification.objects.get(tournament_entry__tournament=t)
                self.assertEqual(verification.tournament_entry.user, game.white)
                self.assertEqual(verification.status, EligibilityVerification.Status.PENDING)

                claim = PrizeClaim.objects.get(tournament=t)
                self.assertEqual((claim.winner, claim.amount, claim.currency), (game.white, Decimal("500.00"), "ILS"))
                self.assertEqual(t.payout_status, Tournament.PayoutStatus.PROCESSING)

                # Only the champion keeps a payout address.
                emails = dict(TournamentParticipant.objects.filter(tournament=t).values_list("user_id", "paypal_email"))
                self.assertTrue(emails[game.white_id])
                self.assertEqual(emails[game.black_id], "")


class PastTournamentsAndHallOfFameTest(TestCase):
    def setUp(self):
        self.champ1 = User.objects.create_user("champ1", password="x")
        self.champ2 = User.objects.create_user("champ2", password="x")
        common = dict(status=Tournament.Status.COMPLETED, is_money_tournament=False, prize_amount=None, terms_version="")
        self.gauntlet = _tournament(
            Tournament.Type.GAUNTLET, "Gladiator Gauntlet Week 7", champion=self.champ1,
            week_number=7, start_time=timezone.now() - timedelta(days=14), **common,
        )
        self.mania = _tournament(
            Tournament.Type.GLADIATORMANIA, "The Gladiatormania Spring Cup", champion=self.champ2,
            week_number=8, start_time=timezone.now() - timedelta(days=7), **common,
        )
        self.current = _tournament(
            Tournament.Type.GAUNTLET, "Gladiator Gauntlet Week 9", is_money_tournament=False,
            prize_amount=None, week_number=9,
        )

    def test_archive_lists_both_types_by_their_own_names(self):
        resp = self.client.get(reverse("tournaments:gauntlet"))
        self.assertContains(resp, "<span>Gladiator Gauntlet Week 7</span>", html=True)
        self.assertContains(resp, "<span>The Gladiatormania Spring Cup</span>", html=True)
        self.assertContains(resp, reverse("tournaments:gauntlet_by_pk", args=[self.mania.pk]))
        self.assertContains(resp, "Gladiator Gauntlet Week 9")  # the current tournament heads the page

    def test_tournament_list_shows_both_names(self):
        resp = self.client.get(reverse("tournaments:list"))
        self.assertContains(resp, "Gladiator Gauntlet Week 7")
        self.assertContains(resp, "The Gladiatormania Spring Cup")

    def test_hall_of_fame_includes_both_types(self):
        resp = self.client.get(reverse("core:home"))
        champions = list(resp.context["gauntlet_champions"])
        self.assertEqual(
            [(t.name, t.champion.username) for t in champions],
            [("The Gladiatormania Spring Cup", "champ2"), ("Gladiator Gauntlet Week 7", "champ1")],
        )


class TournamentAdminTermsDropdownTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("root", "r@x.com", "pw")
        self.client.force_login(self.staff)
        self.active = _terms("dd-active", "2.0")
        self.inactive = _terms("dd-inactive", "1.0", is_active=False)
        self.linked_inactive = _terms("dd-linked", "1.0", is_active=False)

    def _choices(self, resp):
        qs = resp.context["adminform"].form.fields["terms"].queryset
        return set(qs.filter(slug__startswith="dd-"))

    def test_lists_active_records_plus_the_currently_linked_one(self):
        t = _tournament(Tournament.Type.GAUNTLET, "Linked", terms=self.linked_inactive)
        resp = self.client.get(reverse("admin:tournaments_tournament_change", args=[t.pk]))
        self.assertEqual(self._choices(resp), {self.active, self.linked_inactive})

        resp = self.client.get(reverse("admin:tournaments_tournament_add"))
        self.assertEqual(self._choices(resp), {self.active})

    def test_terms_dropdown_is_shown_for_every_type_in_the_admin(self):
        js = ADMIN_JS.read_text(encoding="utf-8")
        self.assertNotIn("termsRow.style.display", js)

        t = _tournament(Tournament.Type.GAUNTLET, "Page")
        resp = self.client.get(reverse("admin:tournaments_tournament_change", args=[t.pk]))
        # The script may be served under a hashed name (manifest storage).
        self.assertRegex(resp.content.decode(), r"tournament_type_fields[\w.]*\.js")
        self.assertContains(resp, 'id="id_terms"')
        self.assertContains(resp, "field-terms")  # the row the script hides

    def _form(self, ttype, terms, **extra):
        request = RequestFactory().get("/")
        request.user = self.staff
        form_class = TournamentAdmin(Tournament, admin.site).get_form(request)
        start = timezone.now() + timedelta(days=2)
        data = {
            "name": "Form", "type": ttype, "game_type": "chess", "time_control": "3+1",
            "capacity": 2, "rounds_total": 1, "current_round": 0, "status": "open",
            "start_time_0": start.strftime("%Y-%m-%d"), "start_time_1": start.strftime("%H:%M:%S"),
            "is_money_tournament": "on", "prize_amount": "500.00", "prize_currency": "ILS",
            "payout_status": "pending", "payout_method": "paypal", "claim_deadline_days": 30,
            "terms": terms.pk if terms else "",
        }
        data.update(extra)
        return form_class(data)

    def test_server_side_every_type_can_keep_a_terms_record(self):
        for ttype in Tournament.Type.values:
            self.assertTrue(self._form(ttype, self.active).is_valid(), ttype)


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False, HF_PLATFORM_TOKEN="hf_test")
class ConfirmationEmailFailureTest(TestCase):
    """Behaviour today: the registration is kept and the failure is logged (no rollback)."""

    def _run(self, failure):
        players = _players(email=True)
        terms = _terms("mail-fail")
        t = _tournament(Tournament.Type.GLADIATORMANIA, "Mail", terms=terms)
        with ExitStack() as stack:
            _external_patches(stack, confirmation_email=True)
            stack.enter_context(failure)
            with self.assertLogs("apps.tournaments.views", level="ERROR") as logs:
                first = _join(players[0], t, terms)
            second = _join(players[1], t, terms)
        return players, terms, t, first, second, logs

    def _assert_join_complete(self, players, terms, t, first, second, logs):
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertTrue(any("T&C email failed entirely" in line for line in logs.output))
        rows = TournamentParticipant.objects.filter(tournament=t)
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual((row.accepted_terms_slug, row.terms_version_accepted), (terms.slug, "1.0"))
            self.assertIsNotNone(row.terms_accepted_at)
            self.assertTrue(row.confirmed_age_18_plus and row.confirmed_israeli_resident)
            self.assertEqual(row.paypal_email, f"{row.user.username}@paypal.test")
        t.refresh_from_db()
        self.assertEqual(t.status, Tournament.Status.ONGOING)  # a failed email never blocks the start
        self.assertEqual(len(mail.outbox), 0)

    def test_pdf_generation_error_keeps_the_registration(self):
        self._assert_join_complete(*self._run(
            mock.patch("apps.tournaments.tasks._build_tc_pdf", side_effect=RuntimeError("pdf boom")),
        ))

    def test_smtp_error_keeps_the_registration(self):
        self._assert_join_complete(*self._run(
            mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("smtp down")),
        ))
