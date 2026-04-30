"""Tests for the anti-cheat SHA audit subsystem.

Covers:
- ``capture_round_baseline`` — pinning baseline at round start.
- ``perform_sha_check`` — PASS / FAIL / ERROR / SKIPPED outcomes.
- Mismatch reaction — disqualification flag + audit row + log line.
- ``run_random_audit_pass`` — Bernoulli scheduler honours probability.
- ``audit_tournament_sha`` management command — manual trigger path.

All HF Hub calls are mocked. The tests never hit the network.
"""
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
CHANGED_SHA = "b" * 40

RESOLVE_PATH = "apps.users.integrity._resolve_ref_sha"


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────
def _make_user(username: str) -> User:
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="x",
    )


def _make_tournament(name: str = "T1", game_type: str = "chess") -> Tournament:
    return Tournament.objects.create(
        name=name,
        type=Tournament.Type.SMALL,
        game_type=game_type,
        status=Tournament.Status.ONGOING,
        start_time=timezone.now(),
        capacity=128,
        rounds_total=7,
        current_round=1,
    )


def _add_participant(tournament, user, *, with_baseline=True, repo="x/y"):
    UserGameModel.objects.create(
        user=user, game_type=tournament.game_type,
        hf_model_repo_id=repo,
        approved_full_sha=BASELINE_SHA,
        last_known_commit_id=BASELINE_SHA,
        original_model_commit_sha=BASELINE_SHA,
        submitted_ref="main",
        submission_repo_type="model",
        is_verified=True,
        model_integrity_ok=True,
        rated_games_played=30,
        rated_games_since_revalidation=30,
    )
    p = TournamentParticipant.objects.create(
        tournament=tournament, user=user, seed=0,
        round_pinned_sha=BASELINE_SHA if with_baseline else "",
        round_pinned_at=timezone.now() if with_baseline else None,
    )
    return p


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  capture_round_baseline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CaptureRoundBaselineTests(TestCase):
    def test_pins_sha_for_active_participants(self):
        from apps.tournaments.sha_audit import capture_round_baseline

        t = _make_tournament()
        u = _make_user("alice")
        p = _add_participant(t, u, with_baseline=False)

        n = capture_round_baseline(t, round_num=1)

        p.refresh_from_db()
        self.assertEqual(n, 1)
        self.assertEqual(p.round_pinned_sha, BASELINE_SHA)
        self.assertIsNotNone(p.round_pinned_at)
        # Baseline-pin row written.
        rows = TournamentShaCheck.objects.filter(
            tournament=t, context=TournamentShaCheck.Context.ROUND_START,
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().result, TournamentShaCheck.Result.PASS)

    def test_qa_tournament_skipped(self):
        from apps.tournaments.sha_audit import capture_round_baseline

        t = _make_tournament()
        t.type = Tournament.Type.QA
        t.save(update_fields=["type"])

        u = _make_user("qa1")
        _add_participant(t, u, with_baseline=False)

        self.assertEqual(capture_round_baseline(t, 1), 0)
        self.assertFalse(TournamentShaCheck.objects.exists())

    def test_eliminated_participants_skipped(self):
        from apps.tournaments.sha_audit import capture_round_baseline

        t = _make_tournament()
        u = _make_user("dead")
        p = _add_participant(t, u, with_baseline=False)
        p.eliminated = True
        p.save(update_fields=["eliminated"])

        self.assertEqual(capture_round_baseline(t, 1), 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  perform_sha_check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@override_settings(HF_PLATFORM_TOKEN="hf_test")
class PerformShaCheckTests(TestCase):

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_pass_when_sha_matches(self, _r, _t):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        u = _make_user("ok")
        p = _add_participant(t, u)

        row = perform_sha_check(p, context="manual", broadcast=False)
        self.assertIsNotNone(row)
        self.assertEqual(row.result, TournamentShaCheck.Result.PASS)
        self.assertEqual(row.expected_sha, BASELINE_SHA)
        self.assertEqual(row.current_sha, BASELINE_SHA)

        p.refresh_from_db()
        self.assertFalse(p.disqualified_for_sha_mismatch)
        self.assertFalse(p.eliminated)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=CHANGED_SHA)
    def test_fail_disqualifies_and_writes_audit(self, _r, _t):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        u = _make_user("cheat")
        p = _add_participant(t, u)

        with mock.patch(
            "apps.tournaments.engine._handle_mid_round_disqualification"
        ) as dq:
            row = perform_sha_check(p, context="random_audit", broadcast=False)

        self.assertEqual(row.result, TournamentShaCheck.Result.FAIL)
        self.assertEqual(row.action_taken, "disqualified_in_round")
        self.assertEqual(row.expected_sha, BASELINE_SHA)
        self.assertEqual(row.current_sha, CHANGED_SHA)
        dq.assert_called_once()

        p.refresh_from_db()
        self.assertTrue(p.disqualified_for_sha_mismatch)

        gm = UserGameModel.objects.get(user=u, game_type="chess")
        self.assertFalse(gm.model_integrity_ok)
        self.assertEqual(gm.rated_games_since_revalidation, 0)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=None)
    def test_error_when_hf_returns_none(self, _r, _t):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        u = _make_user("netfail")
        p = _add_participant(t, u)

        row = perform_sha_check(p, context="manual", broadcast=False)
        self.assertEqual(row.result, TournamentShaCheck.Result.ERROR)
        p.refresh_from_db()
        self.assertFalse(p.disqualified_for_sha_mismatch)

    def test_skipped_without_baseline(self):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        u = _make_user("nobaseline")
        p = _add_participant(t, u, with_baseline=False)
        # Strip the UGM SHA too so no baseline can be derived.
        UserGameModel.objects.filter(user=u).update(
            approved_full_sha="", last_known_commit_id=None,
            original_model_commit_sha=None,
        )

        row = perform_sha_check(p, context="manual", broadcast=False)
        self.assertEqual(row.result, TournamentShaCheck.Result.SKIPPED)

    def test_qa_tournament_returns_none(self):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        t.type = Tournament.Type.QA
        t.save(update_fields=["type"])
        u = _make_user("qa")
        p = _add_participant(t, u)

        self.assertIsNone(perform_sha_check(p, context="manual", broadcast=False))
        self.assertFalse(TournamentShaCheck.objects.exists())

    def test_eliminated_participant_returns_none(self):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        u = _make_user("done")
        p = _add_participant(t, u)
        p.eliminated = True
        p.save(update_fields=["eliminated"])

        self.assertIsNone(perform_sha_check(p, context="manual", broadcast=False))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run_random_audit_pass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@override_settings(HF_PLATFORM_TOKEN="hf_test")
class RandomAuditPassTests(TestCase):

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_high_probability_checks_everyone(self, _r, _t):
        from apps.tournaments.sha_audit import run_random_audit_pass

        t = _make_tournament()
        for n in range(3):
            _add_participant(t, _make_user(f"u{n}"), repo=f"r/{n}")

        # tick=1000s ensures p = min(1, 1000/45..150) = 1.0 → all checked
        summary = run_random_audit_pass(
            tick_seconds=1000.0, async_dispatch=False,
        )
        self.assertEqual(summary["tournaments"], 1)
        self.assertEqual(summary["checked"], 3)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_zero_probability_skips_everyone(self, _r, _t):
        from apps.tournaments.sha_audit import run_random_audit_pass

        t = _make_tournament()
        for n in range(3):
            _add_participant(t, _make_user(f"x{n}"), repo=f"r/{n}")

        # tick=0 → p=0 → nothing rolled in, all skipped
        summary = run_random_audit_pass(
            tick_seconds=0.0, async_dispatch=False,
        )
        self.assertEqual(summary["checked"], 0)
        self.assertEqual(summary["skipped"], 3)

    def test_no_ongoing_tournaments_returns_empty_summary(self):
        from apps.tournaments.sha_audit import run_random_audit_pass

        Tournament.objects.all().delete()
        summary = run_random_audit_pass(
            tick_seconds=30.0, async_dispatch=False,
        )
        self.assertEqual(summary["tournaments"], 0)
        self.assertEqual(summary["checked"], 0)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_disqualified_excluded_from_pass(self, _r, _t):
        from apps.tournaments.sha_audit import run_random_audit_pass

        t = _make_tournament()
        u = _make_user("flagged")
        p = _add_participant(t, u)
        p.disqualified_for_sha_mismatch = True
        p.save(update_fields=["disqualified_for_sha_mismatch"])

        summary = run_random_audit_pass(
            tick_seconds=1000.0, async_dispatch=False,
        )
        self.assertEqual(summary["checked"], 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Management command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@override_settings(HF_PLATFORM_TOKEN="hf_test")
class AuditTournamentShaCommandTests(TestCase):

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_runs_for_single_user(self, _r, _t):
        t = _make_tournament()
        u = _make_user("solo")
        _add_participant(t, u)

        out = StringIO()
        call_command(
            "audit_tournament_sha",
            "--tournament", str(t.pk),
            "--user", str(u.pk),
            stdout=out,
        )
        self.assertIn("pass=1", out.getvalue())

    def test_capture_baseline_flag(self):
        t = _make_tournament()
        u = _make_user("base")
        p = _add_participant(t, u, with_baseline=False)

        out = StringIO()
        call_command(
            "audit_tournament_sha",
            "--tournament", str(t.pk),
            "--capture-baseline",
            stdout=out,
        )
        self.assertIn("Pinned baseline SHA for 1", out.getvalue())
        p.refresh_from_db()
        self.assertEqual(p.round_pinned_sha, BASELINE_SHA)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_random_pass_flag(self, _r, _t):
        t = _make_tournament()
        _add_participant(t, _make_user("rp"))

        out = StringIO()
        call_command(
            "audit_tournament_sha",
            "--random-pass",
            "--tick-seconds", "1000",
            stdout=out,
        )
        self.assertIn("Random pass:", out.getvalue())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async dispatch + 30-rated-games re-qualification gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@override_settings(HF_PLATFORM_TOKEN="hf_test")
class AsyncDispatchTests(TestCase):
    """`run_random_audit_pass` must enqueue Celery tasks rather than
    block the beat tick when ``async_dispatch=True``."""

    def test_random_pass_enqueues_via_delay(self):
        from apps.tournaments.sha_audit import run_random_audit_pass

        t = _make_tournament()
        for n in range(3):
            _add_participant(t, _make_user(f"a{n}"), repo=f"r/{n}")

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            summary = run_random_audit_pass(
                tick_seconds=1000.0, async_dispatch=True,
            )

        # Every candidate was dispatched, none ran inline.
        self.assertEqual(summary["dispatched"], 3)
        self.assertEqual(summary["checked"], 3)
        self.assertEqual(delay.call_count, 3)
        # No audit rows yet — those will be written by the workers.
        self.assertFalse(TournamentShaCheck.objects.exists())

    def test_async_dispatch_falls_back_when_celery_unavailable(self):
        """If `.delay` is missing (e.g. Celery not configured), the pass
        transparently runs checks inline so coverage is never lost."""
        from apps.tournaments.sha_audit import run_random_audit_pass

        t = _make_tournament()
        _add_participant(t, _make_user("fallback"))

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant",
            new=object(),  # has no `.delay` attribute
        ), mock.patch(
            "apps.tournaments.sha_audit._safe_user_token", return_value="t",
        ), mock.patch(
            RESOLVE_PATH, return_value=BASELINE_SHA,
        ):
            summary = run_random_audit_pass(
                tick_seconds=1000.0, async_dispatch=True,
            )

        self.assertEqual(summary["checked"], 1)
        # Inline path persisted a real audit row.
        self.assertEqual(
            TournamentShaCheck.objects.filter(
                result=TournamentShaCheck.Result.PASS,
            ).count(),
            1,
        )


@override_settings(HF_PLATFORM_TOKEN="hf_test")
class ReQualificationGateTests(TestCase):
    """After a SHA-mismatch DQ, the user MUST replay 30 rated games
    before they can rejoin any tournament. ``can_join_tournament``
    enforces this via ``rated_games_since_revalidation`` which we reset
    to 0 on every mismatch."""

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=CHANGED_SHA)
    def test_dq_resets_rated_games_counter_to_zero(self, _r, _t):
        from apps.tournaments.sha_audit import perform_sha_check

        t = _make_tournament()
        u = _make_user("dq_user")
        p = _add_participant(t, u)
        UserGameModel.objects.filter(user=u).update(
            rated_games_since_revalidation=99,  # well past the threshold
        )

        with mock.patch(
            "apps.tournaments.engine._handle_mid_round_disqualification"
        ):
            perform_sha_check(p, context="random_audit", broadcast=False)

        gm = UserGameModel.objects.get(user=u)
        self.assertEqual(gm.rated_games_since_revalidation, 0)
        self.assertFalse(gm.model_integrity_ok)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=CHANGED_SHA)
    def test_can_join_tournament_blocks_until_30_games_replayed(self, _r, _t):
        """End-to-end gate: trigger a mismatch DQ, then call
        ``can_join_tournament`` and assert it refuses until the user
        has logged ``REVALIDATION_GAMES_REQUIRED`` more rated games."""
        from apps.tournaments.sha_audit import perform_sha_check
        from apps.users.integrity import (
            REVALIDATION_GAMES_REQUIRED, can_join_tournament,
        )

        t = _make_tournament()
        u = _make_user("rejoiner")
        p = _add_participant(t, u)
        UserGameModel.objects.filter(user=u).update(
            rated_games_since_revalidation=50,
            locked_commit_id=BASELINE_SHA,  # ensures "repo changed" branch
        )

        # Step 1: random audit catches the SHA change → DQ.
        with mock.patch(
            "apps.tournaments.engine._handle_mid_round_disqualification"
        ):
            perform_sha_check(p, context="random_audit", broadcast=False)

        gm = UserGameModel.objects.get(user=u)
        # Counter was reset; integrity flag is False.
        self.assertEqual(gm.rated_games_since_revalidation, 0)
        self.assertFalse(gm.model_integrity_ok)

        # Step 2: cannot join — model_integrity_ok=False short-circuits
        # can_join_tournament BEFORE it even checks the counter.
        # Note: can_join_tournament also calls validate_model_integrity
        # if a token exists — patch _get_stored_token so it returns None
        # and the function skips the live check, exercising the cached state.
        with mock.patch(
            "apps.users.integrity._get_stored_token", return_value=None,
        ):
            allowed, reason = can_join_tournament(u, "chess")
        self.assertFalse(allowed)
        self.assertIn("integrity", reason.lower())

        # Step 3: simulate the user replaying enough games to clear the
        # cooldown AND a re-approval that flips model_integrity_ok=True.
        UserGameModel.objects.filter(user=u).update(
            rated_games_since_revalidation=REVALIDATION_GAMES_REQUIRED,
            model_integrity_ok=True,
        )

        with mock.patch(
            "apps.users.integrity._get_stored_token", return_value=None,
        ):
            allowed, reason = can_join_tournament(u, "chess")
        self.assertTrue(allowed, msg=f"Expected allowed, got: {reason}")

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=CHANGED_SHA)
    def test_partial_replay_still_blocked(self, _r, _t):
        """Even with model_integrity_ok flipped back on, the user is
        still blocked until they hit the 30-game threshold."""
        from apps.tournaments.sha_audit import perform_sha_check
        from apps.users.integrity import (
            REVALIDATION_GAMES_REQUIRED, can_join_tournament,
        )

        t = _make_tournament()
        u = _make_user("halfway")
        p = _add_participant(t, u)
        UserGameModel.objects.filter(user=u).update(
            locked_commit_id=BASELINE_SHA,
        )

        with mock.patch(
            "apps.tournaments.engine._handle_mid_round_disqualification"
        ):
            perform_sha_check(p, context="random_audit", broadcast=False)

        # User replays only HALF the required games and gets re-approved.
        UserGameModel.objects.filter(user=u).update(
            rated_games_since_revalidation=REVALIDATION_GAMES_REQUIRED // 2,
            model_integrity_ok=True,
        )

        with mock.patch(
            "apps.users.integrity._get_stored_token", return_value=None,
        ):
            allowed, reason = can_join_tournament(u, "chess")
        self.assertFalse(allowed)
        # Reason should reference the rated-games gate.
        self.assertIn("rated", reason.lower())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-round guaranteed integrity check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@override_settings(HF_PLATFORM_TOKEN="hf_test")
class ScheduleRoundIntegrityCheckTests(TestCase):
    """Every round must enqueue ONE randomised integrity check at a
    random offset inside the round window."""

    def test_schedule_uses_random_countdown_in_window(self):
        from apps.tournaments.sha_audit import (
            ROUND_CHECK_DELAY_MAX_SEC,
            ROUND_CHECK_DELAY_MIN_SEC,
            schedule_round_integrity_check,
        )

        t = _make_tournament()
        with mock.patch(
            "apps.tournaments.tasks.run_round_integrity_check.apply_async"
        ) as apply_async:
            delay = schedule_round_integrity_check(t, round_num=1)

        self.assertGreaterEqual(delay, ROUND_CHECK_DELAY_MIN_SEC)
        self.assertLessEqual(delay, ROUND_CHECK_DELAY_MAX_SEC)
        apply_async.assert_called_once()
        kwargs = apply_async.call_args.kwargs
        self.assertEqual(kwargs.get("args"), [t.pk, 1])
        self.assertEqual(kwargs.get("countdown"), delay)

    def test_generate_pairings_schedules_round_check(self):
        """Round 1 of a normal tournament must auto-enqueue a per-round
        integrity check via the engine hook."""
        # Create a real bracket with two participants so generate_pairings
        # can pair them. We patch the bot-game launcher and Game.save
        # signal-heavy path with the lightest possible stubs.
        from apps.tournaments import engine as tengine

        t = Tournament.objects.create(
            name="EngineHook", type=Tournament.Type.SMALL,
            game_type="chess", status=Tournament.Status.OPEN,
            start_time=timezone.now(), capacity=2, rounds_total=1,
            current_round=0,
        )
        u1, u2 = _make_user("e1"), _make_user("e2")
        for u in (u1, u2):
            UserGameModel.objects.create(
                user=u, game_type="chess", hf_model_repo_id=f"r/{u.username}",
                approved_full_sha=BASELINE_SHA,
                last_known_commit_id=BASELINE_SHA,
                original_model_commit_sha=BASELINE_SHA,
                submitted_ref="main", submission_repo_type="model",
                is_verified=True, model_integrity_ok=True,
                rated_games_played=30, rated_games_since_revalidation=30,
            )
            TournamentParticipant.objects.create(
                tournament=t, user=u, seed=0,
            )

        with mock.patch.object(
            tengine, "_start_bot_game_thread"
        ), mock.patch(
            "apps.tournaments.sha_audit.schedule_round_integrity_check"
        ) as sched, mock.patch(
            "apps.tournaments.sha_audit.capture_round_baseline"
        ):
            tengine.generate_pairings(t, round_num=1)

        sched.assert_called_once()
        called_args = sched.call_args.args
        self.assertEqual(called_args[0].pk, t.pk)
        self.assertEqual(called_args[1], 1)


@override_settings(HF_PLATFORM_TOKEN="hf_test")
class RunRoundIntegrityPassTests(TestCase):
    """The Celery-task body that actually runs the per-round checks."""

    def test_dispatches_one_check_per_active_participant(self):
        from apps.tournaments.sha_audit import run_round_integrity_pass

        t = _make_tournament()
        for n in range(3):
            _add_participant(t, _make_user(f"r{n}"), repo=f"r/{n}")

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            summary = run_round_integrity_pass(t.pk, round_num=1)

        self.assertEqual(summary["dispatched"], 3)
        self.assertEqual(delay.call_count, 3)
        self.assertFalse(summary["skipped_stale"])

    def test_skips_when_round_has_advanced(self):
        from apps.tournaments.sha_audit import run_round_integrity_pass

        t = _make_tournament()
        _add_participant(t, _make_user("late"))
        # Round 1 was scheduled, but the tournament is now on round 3.
        t.current_round = 3
        t.save(update_fields=["current_round"])

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            summary = run_round_integrity_pass(t.pk, round_num=1)

        self.assertTrue(summary["skipped_stale"])
        delay.assert_not_called()

    def test_skips_when_tournament_completed(self):
        from apps.tournaments.sha_audit import run_round_integrity_pass

        t = _make_tournament()
        _add_participant(t, _make_user("done"))
        t.status = Tournament.Status.COMPLETED
        t.save(update_fields=["status"])

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            summary = run_round_integrity_pass(t.pk, round_num=1)

        self.assertTrue(summary["skipped_stale"])
        delay.assert_not_called()

    def test_skips_eliminated_and_disqualified_participants(self):
        from apps.tournaments.sha_audit import run_round_integrity_pass

        t = _make_tournament()
        active = _add_participant(t, _make_user("active"), repo="r/a")
        elim = _add_participant(t, _make_user("elim"), repo="r/e")
        elim.eliminated = True
        elim.save(update_fields=["eliminated"])
        dq = _add_participant(t, _make_user("dq"), repo="r/d")
        dq.disqualified_for_sha_mismatch = True
        dq.save(update_fields=["disqualified_for_sha_mismatch"])

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            summary = run_round_integrity_pass(t.pk, round_num=1)

        self.assertEqual(summary["dispatched"], 1)
        delay.assert_called_once_with(active.pk)

    def test_qa_tournament_is_skipped(self):
        from apps.tournaments.sha_audit import run_round_integrity_pass

        t = _make_tournament()
        t.type = Tournament.Type.QA
        t.save(update_fields=["type"])
        _add_participant(t, _make_user("qa"))

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            summary = run_round_integrity_pass(t.pk, round_num=1)

        self.assertTrue(summary["skipped_stale"])
        delay.assert_not_called()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  pre_round_sha_check (restored from no-op)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@override_settings(HF_PLATFORM_TOKEN="hf_test")
class PreRoundShaCheckTests(TestCase):
    """The pre-round hook must (a) dispatch one async check per active
    participant, (b) not block the caller, (c) eliminate users whose
    SHA changed when the worker runs, and (d) trip the 30-rated-games
    re-entry gate."""

    def test_pre_round_dispatches_async_per_active_participant(self):
        from apps.tournaments.engine import pre_round_sha_check

        t = _make_tournament()
        for n in range(3):
            _add_participant(t, _make_user(f"pr{n}"), repo=f"r/{n}")

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay, mock.patch(
            "apps.tournaments.sha_audit.perform_sha_check"
        ) as inline:
            results = pre_round_sha_check(t)

        self.assertEqual(delay.call_count, 3)
        inline.assert_not_called()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r[1] == "dispatched" for r in results))

    def test_pre_round_skips_qa_tournaments(self):
        from apps.tournaments.engine import pre_round_sha_check

        t = _make_tournament()
        t.type = Tournament.Type.QA
        t.save(update_fields=["type"])
        _add_participant(t, _make_user("qa-pr"))

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            results = pre_round_sha_check(t)

        self.assertEqual(results, [])
        delay.assert_not_called()

    def test_pre_round_skips_eliminated_and_disqualified(self):
        from apps.tournaments.engine import pre_round_sha_check

        t = _make_tournament()
        active = _add_participant(t, _make_user("ok"), repo="r/ok")
        elim = _add_participant(t, _make_user("e"), repo="r/e")
        elim.eliminated = True
        elim.save(update_fields=["eliminated"])
        dq = _add_participant(t, _make_user("d"), repo="r/d")
        dq.disqualified_for_sha_mismatch = True
        dq.save(update_fields=["disqualified_for_sha_mismatch"])

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant.delay"
        ) as delay:
            pre_round_sha_check(t)

        self.assertEqual(delay.call_count, 1)
        delay.assert_called_once_with(active.pk)

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=BASELINE_SHA)
    def test_pre_round_inline_fallback_pass_case(self, _r, _t):
        """When Celery's `.delay` is unavailable, the hook runs the
        check inline so enforcement is never silently skipped — and a
        matching SHA leaves the participant active."""
        from apps.tournaments.engine import pre_round_sha_check

        t = _make_tournament()
        p = _add_participant(t, _make_user("pass"))

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant",
            new=object(),  # no `.delay` attribute → fallback
        ):
            results = pre_round_sha_check(t)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "pass")
        self.assertEqual(str(results[0][1]), "pass")
        p.refresh_from_db()
        self.assertFalse(p.eliminated)
        self.assertFalse(p.disqualified_for_sha_mismatch)
        self.assertEqual(
            TournamentShaCheck.objects.filter(
                participant=p,
                result=TournamentShaCheck.Result.PASS,
            ).count(),
            1,
        )

    @mock.patch("apps.tournaments.sha_audit._safe_user_token", return_value="t")
    @mock.patch(RESOLVE_PATH, return_value=CHANGED_SHA)
    def test_pre_round_inline_fallback_eliminates_on_mismatch(self, _r, _t):
        """When the inline path runs and the SHA changed, the
        participant is disqualified, the rated-games counter is reset
        to 0, and ``can_join_tournament`` blocks re-entry until the
        user replays REVALIDATION_GAMES_REQUIRED rated games."""
        from apps.tournaments.engine import pre_round_sha_check
        from apps.users.integrity import (
            REVALIDATION_GAMES_REQUIRED, can_join_tournament,
        )

        t = _make_tournament()
        u = _make_user("cheater")
        p = _add_participant(t, u)
        UserGameModel.objects.filter(user=u).update(
            locked_commit_id=BASELINE_SHA,
            rated_games_since_revalidation=99,
        )

        with mock.patch(
            "apps.tournaments.tasks.run_sha_check_for_participant",
            new=object(),
        ), mock.patch(
            "apps.tournaments.engine._handle_mid_round_disqualification"
        ):
            results = pre_round_sha_check(t)

        self.assertEqual(results[0][0], "cheater")
        self.assertEqual(str(results[0][1]), "fail")
        p.refresh_from_db()
        self.assertTrue(p.disqualified_for_sha_mismatch)

        gm = UserGameModel.objects.get(user=u)
        self.assertEqual(gm.rated_games_since_revalidation, 0)
        self.assertFalse(gm.model_integrity_ok)

        # 30-rated-games re-entry gate is tripped automatically.
        with mock.patch(
            "apps.users.integrity._get_stored_token", return_value=None,
        ):
            allowed, _ = can_join_tournament(u, "chess")
        self.assertFalse(allowed)

        # Replay 30 games + re-approval → join is allowed again.
        UserGameModel.objects.filter(user=u).update(
            rated_games_since_revalidation=REVALIDATION_GAMES_REQUIRED,
            model_integrity_ok=True,
        )
        with mock.patch(
            "apps.users.integrity._get_stored_token", return_value=None,
        ):
            allowed, _ = can_join_tournament(u, "chess")
        self.assertTrue(allowed)
