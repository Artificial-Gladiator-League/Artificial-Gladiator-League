"""The rated-games count restarts only when a new commit is approved or the model repo ID changes."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.users.integrity import record_original_sha
from apps.users.models import UserGameModel

User = get_user_model()

OLD = "a" * 40
NEW = "b" * 40


class PinCounterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pinner", password="pw")
        self.gm = UserGameModel.objects.create(
            user=self.user, game_type="chess", hf_model_repo_id="pinner/chess-model",
            approved_full_sha=OLD, original_model_commit_sha=OLD, last_known_commit_id=OLD,
            model_integrity_ok=True, rated_games_since_revalidation=12,
        )
        tmp = tempfile.TemporaryDirectory(prefix="agl_pin_")
        self.addCleanup(tmp.cleanup)
        self.model_dir = Path(tmp.name)

    def _pin(self, sha, problems=()):
        with mock.patch("apps.games.local_inference.resolve_model_path", return_value=(self.model_dir, None)), \
             mock.patch("apps.games.model_check.check_model", return_value=list(problems)):
            record_original_sha(self.gm, "token", sha=sha)
        self.gm.refresh_from_db()

    def test_rechecking_the_same_commit_keeps_the_count(self):
        self._pin(OLD)
        self.assertEqual(self.gm.rated_games_since_revalidation, 12)
        self.assertEqual(self.gm.approved_full_sha, OLD)

    def test_approving_a_new_commit_restarts_the_count(self):
        self._pin(NEW)
        self.assertEqual(self.gm.rated_games_since_revalidation, 0)
        self.assertEqual(self.gm.approved_full_sha, NEW)

    def test_first_pin_keeps_the_count(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(approved_full_sha="")
        self.gm.refresh_from_db()
        self._pin(NEW)
        self.assertEqual(self.gm.rated_games_since_revalidation, 12)
        self.assertEqual(self.gm.approved_full_sha, NEW)

    def test_failed_check_changes_nothing(self):
        self._pin(NEW, problems=["syntax error"])
        self.assertEqual(self.gm.rated_games_since_revalidation, 12)
        self.assertEqual(self.gm.approved_full_sha, OLD)


class RepoIdChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mover", password="pw")
        self.gm = UserGameModel.objects.create(
            user=self.user, game_type="chess", hf_model_repo_id="mover/old-model",
            approved_full_sha=OLD, original_model_commit_sha=OLD, last_known_commit_id=OLD,
            model_integrity_ok=True, rated_games_since_revalidation=20,
        )
        tmp = tempfile.TemporaryDirectory(prefix="agl_pin_")
        self.addCleanup(tmp.cleanup)
        self.model_dir = Path(tmp.name)

    def test_repo_id_change_resets_count_and_clears_the_approved_sha(self):
        self.gm.hf_model_repo_id = "mover/new-model"
        self.gm.save(update_fields=["hf_model_repo_id"])  # the same call shape the Gladiator tab uses
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.rated_games_since_revalidation, 0)
        self.assertTrue(self.gm.repo_changed)
        self.assertFalse(self.gm.model_integrity_ok)
        self.assertEqual(self.gm.approved_full_sha, "")

    def test_first_verification_after_a_repo_id_change_does_not_reset_the_count_again(self):
        self.gm.hf_model_repo_id = "mover/new-model"
        self.gm.save(update_fields=["hf_model_repo_id"])
        UserGameModel.objects.filter(pk=self.gm.pk).update(rated_games_since_revalidation=3)  # games played meanwhile
        self.gm.refresh_from_db()
        with mock.patch("apps.games.local_inference.resolve_model_path", return_value=(self.model_dir, None)), \
             mock.patch("apps.games.model_check.check_model", return_value=[]):
            record_original_sha(self.gm, "token", sha=NEW)
        self.gm.refresh_from_db()
        self.assertEqual(self.gm.rated_games_since_revalidation, 3)
        self.assertEqual(self.gm.approved_full_sha, NEW)
