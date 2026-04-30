"""Tests for the legacy chess SHA backfill — both the management
command (``pin_chess_model_shas``) and the post-save signal
(``auto_pin_sha_after_first_game``).

All HF Hub calls are mocked — these tests never hit the network.
"""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.users.integrity import REVALIDATION_GAMES_REQUIRED
from apps.users.models import UserGameModel

User = get_user_model()

LATEST_SHA = "c" * 40
RESOLVE_PATH = "apps.users.integrity._resolve_ref_sha"
TOKEN_PATH = "apps.users.integrity._get_stored_token"


def _make_legacy_user(username="legacy", repo="test1978/chess-model"):
    """Create a verified chess UGM with empty approved_full_sha and 30 rated games."""
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="x",
    )
    gm = UserGameModel.objects.create(
        user=user,
        game_type="chess",
        hf_model_repo_id=repo,
        approved_full_sha="",                # legacy: never pinned
        original_model_commit_sha=None,
        last_known_commit_id=None,
        submitted_ref="main",
        submission_repo_type="model",
        is_verified=True,
        rated_games_played=REVALIDATION_GAMES_REQUIRED,
        rated_games_since_revalidation=REVALIDATION_GAMES_REQUIRED,
        model_integrity_ok=False,
    )
    return user, gm


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Management command: pin_chess_model_shas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PinChessModelShasCommandTests(TestCase):

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_writes_three_sha_fields_and_marks_integrity_ok(self, _tok, _resolve):
        _user, gm = _make_legacy_user("alice")

        out = StringIO()
        call_command("pin_chess_model_shas", "--no-email", stdout=out)

        gm.refresh_from_db()
        self.assertEqual(gm.approved_full_sha, LATEST_SHA)
        self.assertEqual(gm.original_model_commit_sha, LATEST_SHA)
        self.assertEqual(gm.last_known_commit_id, LATEST_SHA)
        self.assertTrue(gm.model_integrity_ok)
        self.assertGreaterEqual(
            gm.rated_games_since_revalidation, REVALIDATION_GAMES_REQUIRED,
        )
        self.assertIsNotNone(gm.pinned_at)
        self.assertIn("[ok]", out.getvalue())

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_dry_run_does_not_persist(self, _tok, _resolve):
        _user, gm = _make_legacy_user("bob")

        call_command("pin_chess_model_shas", "--dry-run", "--no-email", stdout=StringIO())

        gm.refresh_from_db()
        self.assertEqual(gm.approved_full_sha, "")
        self.assertFalse(gm.model_integrity_ok)

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_filter_by_user(self, _tok, _resolve):
        _u1, gm1 = _make_legacy_user("u1")
        _u2, gm2 = _make_legacy_user("u2")

        call_command(
            "pin_chess_model_shas", "--user", str(gm1.user_id),
            "--no-email", stdout=StringIO(),
        )

        gm1.refresh_from_db()
        gm2.refresh_from_db()
        self.assertEqual(gm1.approved_full_sha, LATEST_SHA)
        self.assertEqual(gm2.approved_full_sha, "")

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_skips_already_pinned_rows(self, _tok, _resolve):
        _user, gm = _make_legacy_user("carol")
        gm.approved_full_sha = "d" * 40
        gm.save()

        out = StringIO()
        call_command("pin_chess_model_shas", "--no-email", stdout=out)

        self.assertIn("No legacy rows", out.getvalue())
        _resolve.assert_not_called()

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=None)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_unreachable_repo_does_not_persist(self, _tok, _resolve):
        _user, gm = _make_legacy_user("dave")

        call_command("pin_chess_model_shas", "--no-email", stdout=StringIO())

        gm.refresh_from_db()
        self.assertEqual(gm.approved_full_sha, "")

    @override_settings(
        HF_PLATFORM_TOKEN="hf_test_token",
        ADMINS=[("Admin", "admin@example.com")],
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SERVER_EMAIL="server@example.com",
    )
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_sends_admin_summary_email_by_default(self, _tok, _resolve):
        _make_legacy_user("eve")
        mail.outbox = []

        call_command("pin_chess_model_shas", stdout=StringIO())

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn("Pinned 1 chess model SHA", msg.subject)
        self.assertIn("eve", msg.body)

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_excludes_unverified_by_default(self, _tok, _resolve):
        _user, gm = _make_legacy_user("frank")
        gm.is_verified = False
        gm.save()

        call_command("pin_chess_model_shas", "--no-email", stdout=StringIO())
        gm.refresh_from_db()
        self.assertEqual(gm.approved_full_sha, "")

        call_command(
            "pin_chess_model_shas", "--include-unverified",
            "--no-email", stdout=StringIO(),
        )
        gm.refresh_from_db()
        self.assertEqual(gm.approved_full_sha, LATEST_SHA)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signal: auto_pin_sha_after_first_game
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AutoPinAfterFirstGameSignalTests(TestCase):

    def _make_finished_game(self, white, black, winner=None, result="1-0"):
        from apps.games.models import Game
        return Game.objects.create(
            white=white,
            black=black,
            winner=winner or white,
            game_type="chess",
            status=Game.Status.WHITE_WINS,
            result=result,
        )

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_pins_sha_for_legacy_player_after_terminal_game(self, _tok, _resolve):
        white, gm_w = _make_legacy_user("white_player")
        black, gm_b = _make_legacy_user("black_player", repo="test1978/chess-model")

        self._make_finished_game(white, black, winner=white, result="1-0")

        gm_w.refresh_from_db()
        gm_b.refresh_from_db()
        self.assertEqual(gm_w.approved_full_sha, LATEST_SHA)
        self.assertEqual(gm_b.approved_full_sha, LATEST_SHA)
        self.assertTrue(gm_w.model_integrity_ok)
        self.assertTrue(gm_b.model_integrity_ok)

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_does_not_overwrite_existing_sha(self, _tok, _resolve):
        existing = "e" * 40
        white, gm_w = _make_legacy_user("white2")
        black, gm_b = _make_legacy_user("black2")
        gm_w.approved_full_sha = existing
        gm_w.save()

        self._make_finished_game(white, black, winner=white)

        gm_w.refresh_from_db()
        self.assertEqual(gm_w.approved_full_sha, existing)  # untouched
        gm_b.refresh_from_db()
        self.assertEqual(gm_b.approved_full_sha, LATEST_SHA)

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_skips_unfinished_games(self, _tok, _resolve):
        from apps.games.models import Game

        white, gm_w = _make_legacy_user("u_unf_w")
        black, gm_b = _make_legacy_user("u_unf_b")

        Game.objects.create(
            white=white, black=black,
            game_type="chess",
            status=Game.Status.ONGOING,
            result=Game.Result.NONE,
        )

        gm_w.refresh_from_db()
        gm_b.refresh_from_db()
        self.assertEqual(gm_w.approved_full_sha, "")
        self.assertEqual(gm_b.approved_full_sha, "")
        _resolve.assert_not_called()

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=None)
    @mock.patch(TOKEN_PATH, return_value="hf_test_token")
    def test_resolve_failure_leaves_sha_empty(self, _tok, _resolve):
        white, gm_w = _make_legacy_user("u_fail_w")
        black, gm_b = _make_legacy_user("u_fail_b")

        self._make_finished_game(white, black, winner=white)

        gm_w.refresh_from_db()
        gm_b.refresh_from_db()
        self.assertEqual(gm_w.approved_full_sha, "")
        self.assertEqual(gm_b.approved_full_sha, "")

    @override_settings(HF_PLATFORM_TOKEN="hf_test_token")
    @mock.patch(RESOLVE_PATH, return_value=LATEST_SHA)
    @mock.patch(TOKEN_PATH, return_value="")
    def test_no_token_skips_silently(self, _tok, _resolve):
        white, gm_w = _make_legacy_user("u_notok_w")
        black, gm_b = _make_legacy_user("u_notok_b")

        self._make_finished_game(white, black, winner=white)

        gm_w.refresh_from_db()
        gm_b.refresh_from_db()
        self.assertEqual(gm_w.approved_full_sha, "")
        self.assertEqual(gm_b.approved_full_sha, "")
        _resolve.assert_not_called()
