"""Repo File Status panel: the data repo is optional, so a blank one is not looked up."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.users import views
from apps.users.models import UserGameModel

User = get_user_model()


class FileStatusDataRepoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("filer", password="pw")
        tmp = tempfile.TemporaryDirectory(prefix="agl_fs_")
        self.addCleanup(tmp.cleanup)
        self.model_cfg = Path(tmp.name) / "config_model.json"
        self.model_cfg.write_text(json.dumps({"files": ["chess_mcvs.py"]}))
        self.data_cfg = Path(tmp.name) / "config_data.json"
        self.data_cfg.write_text(json.dumps({"files": ["opening_book.txt"]}))
        self.calls = []

    def _gm(self, data_repo):
        return UserGameModel.objects.create(
            user=self.user, game_type="chess", hf_model_repo_id="filer/chess-model",
            hf_data_repo_id=data_repo, approved_full_sha="a" * 40,
        )

    def _status(self, gm):
        def fake_download(repo_id, filename, **kw):
            self.calls.append((repo_id, kw.get("repo_type")))
            return str(self.data_cfg if kw.get("repo_type") == "dataset" else self.model_cfg)

        api = mock.MagicMock()
        api.list_repo_files.return_value = ["config_model.json", "chess_mcvs.py", "opening_book.txt"]
        with mock.patch.object(views, "_HF_AVAILABLE", True), \
             mock.patch.object(views, "HfApi", return_value=api), \
             mock.patch.object(views, "hf_hub_download", side_effect=fake_download):
            return views._build_breakthrough_file_status(self.user, gm)

    def test_blank_data_repo_is_not_looked_up_and_shows_no_error(self):
        status = self._status(self._gm(""))
        self.assertEqual(status["data_error"], "")
        self.assertEqual(status["data_files"], [])
        self.assertEqual(status["model_error"], "")
        self.assertEqual([f["name"] for f in status["model_files"]], ["chess_mcvs.py"])
        self.assertNotIn("dataset", [kind for _repo, kind in self.calls])
        self.assertFalse(any("filer/chess-data" in repo for repo, _kind in self.calls))

    def test_saved_data_repo_is_still_checked(self):
        status = self._status(self._gm("filer/my-data"))
        self.assertIn(("filer/my-data", "dataset"), self.calls)
        self.assertEqual([f["name"] for f in status["data_files"]], ["opening_book.txt"])
        self.assertEqual(status["data_error"], "")
