"""delete_hf_cache_for_user and the cleanup_hf_cache command."""
import os
import stat
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.users.model_lifecycle import cleanup_for_user, delete_hf_cache_for_user
from apps.users.models import UserGameModel

User = get_user_model()


class HfCacheCleanupTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "hf_hub_cache"
        self.root.mkdir()
        self.enterContext(override_settings(
            HF_HUB_CACHE=str(self.root),
            USER_MODELS_BASE_DIR=Path(tmp.name) / "user_models",
        ))
        self.user = User.objects.create_user("owner", password="pw")

    def _folder(self, name, readonly=False):
        d = self.root / name
        (d / "snapshots" / "abc").mkdir(parents=True)
        f = d / "snapshots" / "abc" / "w.bin"
        f.write_text("x")
        if readonly:
            os.chmod(f, stat.S_IREAD)
        return d

    def _gm(self, user, model, data="", game="chess"):
        return UserGameModel.objects.create(
            user=user, game_type=game, hf_model_repo_id=model, hf_data_repo_id=data,
        )

    def test_model_and_dataset_folders_deleted(self):
        self._gm(self.user, "own/model", "own/data")
        m = self._folder("models--own--model")
        d = self._folder("datasets--own--data")
        other = self._folder("models--someone--else")
        removed = delete_hf_cache_for_user(self.user.pk)
        self.assertFalse(m.exists())
        self.assertFalse(d.exists())
        self.assertTrue(other.exists())
        self.assertEqual(len(removed), 2)

    def test_legacy_user_repo_deleted(self):
        self.user.hf_model_repo_id = "own/legacy"
        self.user.save(update_fields=["hf_model_repo_id"])
        m = self._folder("models--own--legacy")
        delete_hf_cache_for_user(self.user.pk)
        self.assertFalse(m.exists())

    def test_shared_repo_kept(self):
        other_user = User.objects.create_user("other", password="pw")
        self._gm(self.user, "shared/model", "shared/data")
        self._gm(other_user, "shared/model", "shared/data", game="breakthrough")
        m = self._folder("models--shared--model")
        d = self._folder("datasets--shared--data")
        self.assertEqual(delete_hf_cache_for_user(self.user.pk), [])
        self.assertTrue(m.exists())
        self.assertTrue(d.exists())

    def test_shared_with_legacy_user_kept(self):
        other_user = User.objects.create_user("other", password="pw", hf_model_repo_id="shared/model")
        self.assertTrue(other_user.pk)
        self._gm(self.user, "shared/model")
        m = self._folder("models--shared--model")
        delete_hf_cache_for_user(self.user.pk)
        self.assertTrue(m.exists())

    def test_missing_folder_is_ok(self):
        self._gm(self.user, "own/model", "own/data")
        self.assertEqual(delete_hf_cache_for_user(self.user.pk), [])
        self.assertEqual(delete_hf_cache_for_user(self.user.pk), [])

    def test_invalid_and_traversal_ids_ignored(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        gm = self._gm(self.user, "own/model")
        for bad in ("../outside", "a/b/c", "noslash", "a b/c", "..\\outside", "../.."):
            UserGameModel.objects.filter(pk=gm.pk).update(hf_model_repo_id=bad, hf_data_repo_id=bad)
            self.assertEqual(delete_hf_cache_for_user(self.user.pk), [], bad)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue(self.root.exists())

    def test_read_only_file_deleted(self):
        self._gm(self.user, "own/model")
        m = self._folder("models--own--model", readonly=True)
        delete_hf_cache_for_user(self.user.pk)
        self.assertFalse(m.exists())

    def test_cleanup_for_user_deletes_cache(self):
        self._gm(self.user, "own/model", "own/data")
        m = self._folder("models--own--model")
        d = self._folder("datasets--own--data")
        cleanup_for_user(self.user.pk)
        self.assertFalse(m.exists())
        self.assertFalse(d.exists())

    def test_logout_signal_deletes_cache_and_user_dir(self):
        from django.contrib.auth.signals import user_logged_out

        self._gm(self.user, "own/model", "own/data")
        m = self._folder("models--own--model")
        user_dir = Path(self.root.parent) / "user_models" / f"user_{self.user.pk}"
        (user_dir / "chess" / "data_cache").mkdir(parents=True, exist_ok=True)
        user_logged_out.send(sender=User, request=None, user=self.user)
        self.assertFalse(m.exists())
        self.assertFalse(user_dir.exists())

    def test_command_dry_run_and_delete(self):
        self._gm(self.user, "own/model", "own/data")
        used_m = self._folder("models--own--model")
        used_d = self._folder("datasets--own--data")
        stale_m = self._folder("models--gone--model")
        stale_d = self._folder("datasets--gone--data")

        out = StringIO()
        call_command("cleanup_hf_cache", "--dry-run", stdout=out)
        self.assertTrue(stale_m.exists() and stale_d.exists())
        self.assertIn("models--gone--model", out.getvalue())

        call_command("cleanup_hf_cache", stdout=StringIO())
        self.assertFalse(stale_m.exists())
        self.assertFalse(stale_d.exists())
        self.assertTrue(used_m.exists() and used_d.exists())
