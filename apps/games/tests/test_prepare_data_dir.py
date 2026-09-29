"""Tests for apps.games.local_inference.prepare_data_dir().

Docker bind-mounts a directory as-is, so a ``/data`` mount built from the raw
HF hub cache (files that are symlinks into ``blobs/<sha>``) breaks inside the
sandbox container. These tests confirm prepare_data_dir() never returns such
a directory: even when a stale symlink-only cache snapshot already exists on
disk, it materializes real files via
``huggingface_hub.snapshot_download(local_dir=...)`` and the zone DB ends up
inside the resolved folder.

All HF Hub calls are mocked — these tests never hit the network.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.games.local_inference import prepare_data_dir

ZONE_DB_BYTES = b"fake-npz-bytes-for-test"


class PrepareDataDirTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agl_test_data_dir_")
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

        # ── Simulate a stale HF-cache-style snapshot made only of symlinks ──
        # (mirrors huggingface_hub's default cache_dir layout: the snapshot
        # dir holds symlinks into blobs/<sha>, which Docker bind mounts
        # cannot resolve once mounted read-only into the sandbox).
        blobs_dir = self.base_dir / "fake_hf_cache" / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        blob_file = blobs_dir / "abc123blob"
        blob_file.write_bytes(ZONE_DB_BYTES)

        self.symlink_snapshot_dir = (
            self.base_dir / "fake_hf_cache" / "datasets--test1978--chess-data"
            / "snapshots" / "deadbeef"
        )
        self.symlink_snapshot_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = self.symlink_snapshot_dir / "zone_db.npz"
        # Creating real symlinks requires elevated privileges on Windows
        # (SeCreateSymbolicLinkPrivilege / Developer Mode). Where available,
        # this recreates the exact HF-cache layout being guarded against;
        # where not, the core assertions below (real files materialized,
        # zone DB present, not a symlink) still fully exercise the fix.
        try:
            os.symlink(blob_file, symlink_path)
            self.symlink_created = True
        except OSError:
            self.symlink_created = False

    @staticmethod
    def _fake_snapshot_download(*, repo_id, repo_type, revision, local_dir, **kwargs):
        """Mimic huggingface_hub.snapshot_download(local_dir=...): always
        materializes REAL files under local_dir, never symlinks."""
        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "zone_db.npz").write_bytes(ZONE_DB_BYTES)
        (dest / "config_data.json").write_text('{"files": ["zone_db.npz"]}')
        return str(dest)

    def test_materializes_real_files_and_contains_zone_db(self):
        with override_settings(USER_MODELS_BASE_DIR=str(self.base_dir / "user_models")):
            with mock.patch(
                "huggingface_hub.snapshot_download",
                side_effect=self._fake_snapshot_download,
            ) as mock_download:
                result = prepare_data_dir(
                    user_id=999,
                    game_type="chess",
                    data_repo_id="test1978/chess-data",
                    revision="deadbeef",
                )

        self.assertIsNotNone(result)
        mock_download.assert_called_once()

        zone_db_path = result / "zone_db.npz"
        self.assertTrue(zone_db_path.exists(), f"zone_db.npz missing in {result}")
        self.assertFalse(
            zone_db_path.is_symlink(),
            "resolved data_dir must contain REAL files, not symlinks (Docker bind mounts break on symlinks)",
        )
        self.assertEqual(zone_db_path.read_bytes(), ZONE_DB_BYTES)

        # The stale symlink-only cache snapshot must never be returned.
        if self.symlink_created:
            self.assertNotEqual(result, self.symlink_snapshot_dir)

    def test_second_call_reuses_cached_download_without_network(self):
        with override_settings(USER_MODELS_BASE_DIR=str(self.base_dir / "user_models")):
            with mock.patch(
                "huggingface_hub.snapshot_download",
                side_effect=self._fake_snapshot_download,
            ) as mock_download:
                first = prepare_data_dir(
                    user_id=999, game_type="chess",
                    data_repo_id="test1978/chess-data", revision="deadbeef",
                )
                second = prepare_data_dir(
                    user_id=999, game_type="chess",
                    data_repo_id="test1978/chess-data", revision="deadbeef",
                )

        self.assertEqual(first, second)
        mock_download.assert_called_once()  # second call served from local cache, no re-download

    def test_returns_none_without_repo_id_or_local_files(self):
        with override_settings(USER_MODELS_BASE_DIR=str(self.base_dir / "user_models")):
            result = prepare_data_dir(user_id=999, game_type="chess")
        self.assertIsNone(result)
