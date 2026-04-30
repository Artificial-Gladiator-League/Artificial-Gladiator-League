"""Tests for HF repo SHA integrity validation.

Covers the unified live_sha_check() helper used by:
  • Tournament join (apps.tournaments.views.join_tournament)
  • Pre-round gate (apps.tournaments.tasks.run_pre_tournament_integrity_checks)
  • Mid-game spot-check (apps.games.consumers.GameConsumer._bot_make_move)

All HF Hub calls are mocked — these tests never hit the network.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.users.integrity import live_sha_check, REVALIDATION_GAMES_REQUIRED
from apps.users.models import UserGameModel

User = get_user_model()

APPROVED_SHA = "a" * 40
NEW_SHA = "b" * 40


class LiveShaCheckTests(TestCase):
    """Unit tests for live_sha_check() — the single source of truth."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            password="x",
        )
        self.gm = UserGameModel.objects.create(
            user=self.user,
            game_type="chess",
            hf_model_repo_id="test1978/chess-model",
            approved_full_sha=APPROVED_SHA,
            original_model_commit_sha=APPROVED_SHA,
            last_known_commit_id=APPROVED_SHA,
            submitted_ref="main",
            submission_repo_type="model",
            model_integrity_ok=True,
            rated_games_since_revalidation=REVALIDATION_GAMES_REQUIRED,
            is_verified=True,
        )

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_match_returns_ok_and_does_not_reset_counter(self, mock_resolve):
        mock_resolve.return_value = APPROVED_SHA

        ok, db_sha, hf_sha = live_sha_check(self.gm, context="join")

        self.assertTrue(ok)
        self.assertEqual(db_sha, APPROVED_SHA)
        self.assertEqual(hf_sha, APPROVED_SHA)
        mock_resolve.assert_called_once()
        self.gm.refresh_from_db()
        self.assertTrue(self.gm.model_integrity_ok)
        self.assertEqual(
            self.gm.rated_games_since_revalidation, REVALIDATION_GAMES_REQUIRED,
        )

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_mismatch_resets_counter_and_marks_integrity_bad(self, mock_resolve):
        mock_resolve.return_value = NEW_SHA

        ok, db_sha, hf_sha = live_sha_check(self.gm, context="join")

        self.assertFalse(ok)
        self.assertEqual(db_sha, APPROVED_SHA)
        self.assertEqual(hf_sha, NEW_SHA)
        self.gm.refresh_from_db()
        self.assertFalse(self.gm.model_integrity_ok)
        self.assertEqual(self.gm.last_known_commit_id, NEW_SHA)
        self.assertEqual(self.gm.rated_games_since_revalidation, 0)

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_hf_unreachable_fail_open_does_not_reset(self, mock_resolve):
        mock_resolve.return_value = None  # network failure

        ok, db_sha, hf_sha = live_sha_check(
            self.gm, context="mid-game", fail_open=True,
        )

        self.assertTrue(ok)  # fail-open: don't punish honest users
        self.assertEqual(db_sha, APPROVED_SHA)
        self.assertIsNone(hf_sha)
        self.gm.refresh_from_db()
        self.assertTrue(self.gm.model_integrity_ok)
        self.assertEqual(
            self.gm.rated_games_since_revalidation, REVALIDATION_GAMES_REQUIRED,
        )

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_hf_unreachable_fail_closed_blocks(self, mock_resolve):
        mock_resolve.return_value = None

        ok, _, _ = live_sha_check(self.gm, context="join", fail_open=False)
        self.assertFalse(ok)

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_emits_standardized_print_log(self, mock_resolve):
        """The print(...) line must be emitted regardless of LOGGING config."""
        mock_resolve.return_value = APPROVED_SHA

        with mock.patch("builtins.print") as mock_print:
            live_sha_check(self.gm, context="pre-round")

        # Find the standardized line
        printed = [str(c.args[0]) for c in mock_print.call_args_list if c.args]
        self.assertTrue(
            any(
                "HF API check for repo test1978/chess-model" in p
                and f"current_sha={APPROVED_SHA}" in p
                and f"latest={APPROVED_SHA}" in p
                for p in printed
            ),
            f"Standardized log line not found in printed lines: {printed}",
        )

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_no_baseline_records_first_sha_without_blocking(self, mock_resolve):
        # Brand-new model, no SHA pinned yet
        self.gm.approved_full_sha = ""
        self.gm.original_model_commit_sha = None
        self.gm.last_known_commit_id = None
        self.gm.save()
        mock_resolve.return_value = NEW_SHA

        ok, db_sha, hf_sha = live_sha_check(self.gm, context="join")

        self.assertTrue(ok)
        # First-time baseline: the function records and returns the new SHA.
        self.assertEqual(db_sha, NEW_SHA)
        self.assertEqual(hf_sha, NEW_SHA)
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.last_known_commit_id, NEW_SHA)
        self.assertTrue(self.gm.model_integrity_ok)

    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_missing_repo_id_skips_check(self, mock_resolve):
        self.gm.hf_model_repo_id = ""
        self.gm.save()

        ok, db_sha, hf_sha = live_sha_check(self.gm, context="join")

        self.assertTrue(ok)
        self.assertIsNone(hf_sha)
        mock_resolve.assert_not_called()


class JoinTournamentShaGateTests(TestCase):
    """End-to-end test for the join_tournament SHA gate."""

    def setUp(self):
        from apps.tournaments.models import Tournament
        from django.utils import timezone
        from datetime import timedelta

        self.user = User.objects.create_user(
            username="bob", email="bob@example.com", password="x",
        )
        self.gm = UserGameModel.objects.create(
            user=self.user,
            game_type="chess",
            hf_model_repo_id="test1978/chess-model",
            approved_full_sha=APPROVED_SHA,
            original_model_commit_sha=APPROVED_SHA,
            last_known_commit_id=APPROVED_SHA,
            submitted_ref="main",
            submission_repo_type="model",
            model_integrity_ok=True,
            rated_games_since_revalidation=REVALIDATION_GAMES_REQUIRED,
            is_verified=True,
            locked_commit_id=APPROVED_SHA,
        )
        self.tournament = Tournament.objects.create(
            name="Test Cup",
            type=Tournament.Type.SMALL,
            game_type="chess",
            status=Tournament.Status.OPEN,
            capacity=4,
            start_time=timezone.now() + timedelta(days=1),
        )

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch("apps.users.integrity._resolve_ref_sha")
    def test_join_blocked_with_redirect_on_sha_mismatch(self, mock_resolve):
        from apps.tournaments.models import TournamentParticipant

        mock_resolve.return_value = NEW_SHA

        self.client.force_login(self.user)
        response = self.client.post(
            f"/tournaments/{self.tournament.pk}/join/",
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/tournaments/", response.url)
        # User must NOT have been registered.
        self.assertFalse(
            TournamentParticipant.objects.filter(
                tournament=self.tournament, user=self.user,
            ).exists()
        )
        # And the integrity counter must have been reset.
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.rated_games_since_revalidation, 0)
        self.assertFalse(self.gm.model_integrity_ok)
