"""Revision-aware snapshot handling in apps.games.model_preloader.

Regression: an old complete snapshot (refs/main) must not be accepted for an
approved commit SHA whose own snapshot folder only holds config_model.json.
No network is used; snapshot_download is mocked.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.games import model_preloader
from apps.users.models import UserGameModel

User = get_user_model()

REPO = "test1978/chess-model"
OLD_SHA = "3f810e57" + "a" * 32
APPROVED_SHA = "9e67e13b0453153c54accc6d03d3f6c510824ca2"
SNAPSHOT_DOWNLOAD = "huggingface_hub.snapshot_download"


class PreloaderTestBase(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agl_test_preloader_")
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "hf_hub_cache"
        self.models_base = Path(tmp.name) / "user_models"
        self.enterContext(override_settings(HF_HUB_CACHE=str(self.cache), USER_MODELS_BASE_DIR=self.models_base))
        token = mock.patch("apps.games.model_preloader._resolve_hf_token", return_value="tok")
        token.start()
        self.addCleanup(token.stop)

        self.repo_dir = self.cache / ("models--" + REPO.replace("/", "--"))
        self.user = User.objects.create_user("yuval", password="pw")

    def _snapshot(self, sha: str, files=("config_model.json", "chess_mcvs.py")) -> Path:
        snap = self.repo_dir / "snapshots" / sha
        snap.mkdir(parents=True, exist_ok=True)
        for name in files:
            (snap / name).write_text("{}" if name.endswith(".json") else "def load(ctx): return {}\n")
        return snap

    def _set_main(self, sha: str) -> None:
        refs = self.repo_dir / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(sha)

    def _stale_cache(self) -> Path:
        """Old complete snapshot = refs/main; approved snapshot holds only the config."""
        old = self._snapshot(OLD_SHA)
        self._snapshot(APPROVED_SHA, files=("config_model.json",))
        self._set_main(OLD_SHA)
        return old

    def _gm(self, **over) -> UserGameModel:
        fields = dict(
            user=self.user, game_type="chess", hf_model_repo_id=REPO,
            approved_full_sha=APPROVED_SHA,
        )
        fields.update(over)
        return UserGameModel.objects.create(**fields)

    def _download_fills_approved(self, **_kwargs):
        return str(self._snapshot(APPROVED_SHA))


class CachedSnapshotForRevisionTests(PreloaderTestBase):
    def test_old_snapshot_not_accepted_for_approved_sha(self):
        self._stale_cache()
        self.assertIsNone(model_preloader._cached_snapshot_for_revision(REPO, APPROVED_SHA))

    def test_config_only_snapshot_is_incomplete(self):
        self._snapshot(APPROVED_SHA, files=("config_model.json",))
        self.assertIsNone(model_preloader._cached_snapshot_for_revision(REPO, APPROVED_SHA))

    def test_agl_helper_files_do_not_count(self):
        self._snapshot(APPROVED_SHA, files=("config_model.json", "_agl_runner.py"))
        self.assertIsNone(model_preloader._cached_snapshot_for_revision(REPO, APPROVED_SHA))

    def test_missing_approved_folder_does_not_fall_back_to_newest(self):
        self._snapshot(OLD_SHA)
        self._set_main(OLD_SHA)
        self.assertIsNone(model_preloader._cached_snapshot_for_revision(REPO, APPROVED_SHA))

    def test_short_sha_folder_is_not_a_match(self):
        self._snapshot(APPROVED_SHA[:12])
        self.assertIsNone(model_preloader._cached_snapshot_for_revision(REPO, APPROVED_SHA))

    def test_complete_approved_snapshot_is_accepted(self):
        self._snapshot(OLD_SHA)
        snap = self._snapshot(APPROVED_SHA)
        self.assertEqual(model_preloader._cached_snapshot_for_revision(REPO, APPROVED_SHA), snap)

    def test_non_sha_ref_keeps_previous_behaviour(self):
        old = self._snapshot(OLD_SHA)
        self._set_main(OLD_SHA)
        self.assertEqual(model_preloader._cached_snapshot_for_revision(REPO, "main"), old)


class EnsureHfSnapshotTests(PreloaderTestBase):
    def test_stale_cache_triggers_download_of_approved_sha(self):
        old = self._stale_cache()
        gm = self._gm()
        with mock.patch(SNAPSHOT_DOWNLOAD, side_effect=self._download_fills_approved) as dl:
            snap = model_preloader.ensure_hf_snapshot(gm)
        dl.assert_called_once()
        kwargs = dl.call_args.kwargs
        self.assertEqual(kwargs["repo_id"], REPO)
        self.assertEqual(kwargs["revision"], APPROVED_SHA)
        self.assertEqual(kwargs["cache_dir"], str(self.cache))
        self.assertEqual(kwargs["token"], "tok")
        self.assertEqual(snap.name, APPROVED_SHA)
        self.assertNotEqual(snap, old)

    def test_failed_download_never_returns_old_snapshot(self):
        self._stale_cache()
        gm = self._gm()
        with mock.patch(SNAPSHOT_DOWNLOAD, side_effect=RuntimeError("boom")) as dl:
            with self.assertLogs("apps.games.model_preloader", level="ERROR") as logs:
                snap = model_preloader.ensure_hf_snapshot(gm)
        dl.assert_called_once()
        self.assertIsNone(snap)
        self.assertTrue(any("MODEL-DOWNLOAD-FAILED" in line for line in logs.output))

    def test_download_that_stays_incomplete_is_rejected(self):
        self._stale_cache()
        gm = self._gm()
        partial = str(self.repo_dir / "snapshots" / APPROVED_SHA)
        with mock.patch(SNAPSHOT_DOWNLOAD, return_value=partial):
            with self.assertLogs("apps.games.model_preloader", level="ERROR"):
                self.assertIsNone(model_preloader.ensure_hf_snapshot(gm))

    def test_complete_approved_snapshot_skips_download(self):
        self._snapshot(OLD_SHA)
        approved = self._snapshot(APPROVED_SHA)
        gm = self._gm()
        with mock.patch(SNAPSHOT_DOWNLOAD) as dl:
            self.assertEqual(model_preloader.ensure_hf_snapshot(gm), approved)
        dl.assert_not_called()

    def test_main_ref_with_cached_snapshot_skips_download(self):
        old = self._snapshot(OLD_SHA)
        self._set_main(OLD_SHA)
        gm = self._gm(approved_full_sha="", submitted_ref="main")
        with mock.patch(SNAPSHOT_DOWNLOAD) as dl:
            self.assertEqual(model_preloader.ensure_hf_snapshot(gm), old)
        dl.assert_not_called()


class PreloadUserModelsTests(PreloaderTestBase):
    def test_cached_fields_point_at_approved_snapshot(self):
        old = self._stale_cache()
        gm = self._gm(cached_path=str(old), cached_commit=OLD_SHA)
        with mock.patch(SNAPSHOT_DOWNLOAD, side_effect=self._download_fills_approved) as dl:
            model_preloader.preload_user_models(self.user.pk)
        dl.assert_called_once()
        self.assertEqual(dl.call_args.kwargs["revision"], APPROVED_SHA)
        gm.refresh_from_db()
        self.assertEqual(gm.cached_commit, APPROVED_SHA)
        self.assertEqual(Path(gm.cached_path).name, APPROVED_SHA)
        # The shared cache keeps the other snapshot and the local ref untouched.
        self.assertTrue((old / "chess_mcvs.py").exists())
        self.assertEqual((self.repo_dir / "refs" / "main").read_text(), OLD_SHA)

    def test_failed_download_does_not_record_old_snapshot(self):
        self._stale_cache()
        gm = self._gm()
        with mock.patch(SNAPSHOT_DOWNLOAD, side_effect=RuntimeError("boom")):
            with self.assertLogs("apps.games.model_preloader", level="WARNING"):
                model_preloader.preload_user_models(self.user.pk)
        gm.refresh_from_db()
        self.assertNotEqual(gm.cached_commit, OLD_SHA)
        self.assertNotIn(OLD_SHA, gm.cached_path)

    def test_complete_approved_snapshot_needs_no_download(self):
        self._snapshot(OLD_SHA)
        self._snapshot(APPROVED_SHA)
        gm = self._gm()
        with mock.patch(SNAPSHOT_DOWNLOAD) as dl:
            model_preloader.preload_user_models(self.user.pk)
        dl.assert_not_called()
        gm.refresh_from_db()
        self.assertEqual(gm.cached_commit, APPROVED_SHA)
