"""Tests for ``apps.tournaments.lifecycle.ensure_tournament_integrity``
and the ``fix_stuck_tournaments`` management command."""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tournaments.models import (
    Tournament, TournamentParticipant, TournamentShaCheck,
)
from apps.users.models import UserGameModel

User = get_user_model()

BASELINE_SHA = "a" * 40
RESOLVE_PATH = "apps.users.integrity._resolve_ref_sha"


def _user(username):
    return User.objects.create_user(
        username=username, email=f"{username}@e.com", password="x",
    )


def _tournament(**kw):
    defaults = dict(
        name="T", type=Tournament.Type.SMALL, game_type="chess",
        status=Tournament.Status.OPEN, start_time=timezone.now(),
        capacity=4, rounds_total=2, current_round=0,
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


def _add_pp(t, u, *, baseline=BASELINE_SHA):
    UserGameModel.objects.create(
        user=u, game_type=t.game_type,
        hf_model_repo_id=f"r/{u.username}",
        approved_full_sha=baseline, last_known_commit_id=baseline,
        original_model_commit_sha=baseline, submitted_ref="main",
        submission_repo_type="model", is_verified=True,
        model_integrity_ok=True, rated_games_played=30,
        rated_games_since_revalidation=30,
    )
    return TournamentParticipant.objects.create(
        tournament=t, user=u, seed=0,
    )


@override_settings(HF_PLATFORM_TOKEN="hf_test")
class EnsureTournamentIntegrityTests(TestCase):

    def test_open_with_two_players_past_start_auto_starts(self):
        """OPEN tournament past its start_time with ≥2 players gets
        promoted to ONGOING and round 1 is generated."""
        from apps.tournaments.lifecycle import ensure_tournament_integrity

        t = _tournament(
            status=Tournament.Status.OPEN,
            start_time=timezone.now() - timezone.timedelta(minutes=5),
        )
        for n in range(2):
            _add_pp(t, _user(f"o{n}"))

        with mock.patch(
            "apps.tournaments.engine._start_bot_game_thread"
        ), mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            report = ensure_tournament_integrity(t)

        t.refresh_from_db()
        self.assertEqual(t.status, Tournament.Status.ONGOING)
        self.assertEqual(t.current_round, 1)
        self.assertIn("started_tournament", report["actions"])
        # SHA enforcement was armed for both participants.
        self.assertGreaterEqual(delay.call_count, 2)

    def test_ongoing_with_round_zero_generates_round_one(self):
        """If somehow status=ONGOING but current_round=0 the recovery
        should generate the missing round-1 pairings."""
        from apps.tournaments.lifecycle import ensure_tournament_integrity

        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=0,
        )
        for n in range(2):
            _add_pp(t, _user(f"z{n}"))

        with mock.patch(
            "apps.tournaments.engine._start_bot_game_thread"
        ), mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ):
            report = ensure_tournament_integrity(t)

        t.refresh_from_db()
        self.assertEqual(t.current_round, 1)
        self.assertIn("generated_round_1", report["actions"])
        # Baseline rows now exist for round 1.
        self.assertTrue(
            TournamentShaCheck.objects.filter(
                tournament=t, round_num=1,
                context=TournamentShaCheck.Context.ROUND_START,
            ).exists()
        )

    def test_ongoing_with_round_one_but_no_baseline_recovers(self):
        """ONGOING tournament with matches but missing baseline rows
        should get baseline pinned + audit dispatched."""
        from apps.tournaments.lifecycle import ensure_tournament_integrity

        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t, _user(f"r{n}"))

        # No baseline rows exist yet.
        self.assertFalse(
            TournamentShaCheck.objects.filter(tournament=t).exists()
        )

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            report = ensure_tournament_integrity(t)

        self.assertIn("recovered_baseline_round_1", report["actions"])
        self.assertEqual(
            TournamentShaCheck.objects.filter(
                tournament=t, round_num=1,
                context=TournamentShaCheck.Context.ROUND_START,
            ).count(),
            2,
        )
        # And the audit chain was armed.
        self.assertGreaterEqual(delay.call_count, 2)

    def test_already_healthy_tournament_is_idempotent(self):
        """Calling the recovery on a fully baselined ONGOING tournament
        should NOT re-pin baselines (no duplicates) but SHOULD re-arm
        the audit chain (idempotent dispatch)."""
        from apps.tournaments.lifecycle import ensure_tournament_integrity
        from apps.tournaments.sha_audit import capture_round_baseline

        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t, _user(f"h{n}"))
        capture_round_baseline(t, 1)
        baseline_count_before = TournamentShaCheck.objects.filter(
            tournament=t,
            context=TournamentShaCheck.Context.ROUND_START,
        ).count()

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            report = ensure_tournament_integrity(t)

        baseline_count_after = TournamentShaCheck.objects.filter(
            tournament=t,
            context=TournamentShaCheck.Context.ROUND_START,
        ).count()
        # No duplicate baselines.
        self.assertEqual(baseline_count_before, baseline_count_after)
        # Audit chain was re-armed.
        self.assertGreaterEqual(delay.call_count, 2)
        self.assertNotIn("recovered_baseline_round_1", report["actions"])

    def test_qa_tournament_is_skipped(self):
        from apps.tournaments.lifecycle import ensure_tournament_integrity

        t = _tournament(type=Tournament.Type.QA)
        _add_pp(t, _user("qa"))

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            report = ensure_tournament_integrity(t)

        self.assertIn("skipped_qa", report["actions"])
        delay.assert_not_called()

    def test_tournament_with_zero_participants_is_safe(self):
        """An empty OPEN tournament must NOT raise and must NOT start."""
        from apps.tournaments.lifecycle import ensure_tournament_integrity

        t = _tournament(status=Tournament.Status.OPEN)
        report = ensure_tournament_integrity(t)
        t.refresh_from_db()
        self.assertEqual(t.status, Tournament.Status.OPEN)
        self.assertNotIn("started_tournament", report["actions"])

    def test_step_failures_are_isolated(self):
        """If one recovery step raises, the others must still run and
        the report must capture the failure without crashing."""
        from apps.tournaments.lifecycle import ensure_tournament_integrity

        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t, _user(f"f{n}"))

        # Force capture_round_baseline to blow up.
        with mock.patch(
            "apps.tournaments.sha_audit.capture_round_baseline",
            side_effect=RuntimeError("kaboom"),
        ), mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            report = ensure_tournament_integrity(t)

        # Baseline step crashed, but the audit chain was still armed.
        self.assertNotIn("recovered_baseline_round_1", report["actions"])
        self.assertGreaterEqual(delay.call_count, 2)


@override_settings(HF_PLATFORM_TOKEN="hf_test")
class EnsureTournamentIntegrityCeleryTaskTests(TestCase):

    def test_celery_task_returns_report(self):
        from apps.tournaments.tasks import ensure_tournament_integrity

        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t, _user(f"c{n}"))

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ):
            report = ensure_tournament_integrity.run(t.pk)

        self.assertEqual(report["tournament_id"], t.pk)
        self.assertIn("recovered_baseline_round_1", report["actions"])

    def test_celery_task_handles_missing_tournament(self):
        from apps.tournaments.tasks import ensure_tournament_integrity

        report = ensure_tournament_integrity.run(99999)
        self.assertIn("tournament_not_found", report["errors"])


@override_settings(HF_PLATFORM_TOKEN="hf_test")
class FixStuckTournamentsCommandTests(TestCase):

    def test_command_sweeps_open_and_ongoing(self):
        t1 = _tournament(
            name="OnRound1", status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t1, _user(f"a{n}"))

        t2 = _tournament(
            name="OpenOnly", status=Tournament.Status.OPEN,
        )

        out = StringIO()
        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ):
            call_command("fix_stuck_tournaments", stdout=out)

        output = out.getvalue()
        self.assertIn("Scanned", output)
        self.assertIn("OnRound1", output)
        self.assertIn("recovered_baseline_round_1", output)

    def test_command_targeted_tournament(self):
        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t, _user(f"t{n}"))

        out = StringIO()
        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ):
            call_command(
                "fix_stuck_tournaments",
                "--tournament", str(t.pk),
                stdout=out,
            )

        output = out.getvalue()
        self.assertIn(f"#{t.pk}", output)
        self.assertEqual(
            TournamentShaCheck.objects.filter(
                tournament=t,
                context=TournamentShaCheck.Context.ROUND_START,
            ).count(),
            2,
        )

    def test_command_dry_run_rolls_back(self):
        t = _tournament(
            status=Tournament.Status.ONGOING, current_round=1,
        )
        for n in range(2):
            _add_pp(t, _user(f"d{n}"))

        out = StringIO()
        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ):
            call_command(
                "fix_stuck_tournaments",
                "--tournament", str(t.pk),
                "--dry-run",
                stdout=out,
            )

        output = out.getvalue()
        self.assertIn("DRY RUN", output)
        # No baseline rows should persist.
        self.assertEqual(
            TournamentShaCheck.objects.filter(tournament=t).count(),
            0,
        )
