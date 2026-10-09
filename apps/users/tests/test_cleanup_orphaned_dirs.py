"""cleanup_orphaned_dirs only removes idle users' user_<id> dirs; ongoing-game guard on HF cache."""
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from apps.games.models import Game
from apps.users.model_lifecycle import cleanup_orphaned_dirs, delete_hf_cache_for_user
from apps.users.models import UserGameModel

User = get_user_model()


class CleanupOrphanedDirsTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "user_models"
        self.hf = self.base / "hf_hub_cache"
        self.hf.mkdir(parents=True)
        self.enterContext(override_settings(
            USER_MODELS_BASE_DIR=self.base, MODEL_CACHE_ROOT=self.base, HF_HUB_CACHE=str(self.hf),
        ))
        self.idle = User.objects.create_user("idle", password="pw")
        self.online = User.objects.create_user("online", password="pw")
        self.player = User.objects.create_user("player", password="pw")
        self.rival = User.objects.create_user("rival", password="pw")

    def _dir(self, name):
        d = self.base / name
        (d / "chess" / "model").mkdir(parents=True, exist_ok=True)
        (d / "chess" / "model" / "w.bin").write_text("x")
        return d

    def _repo(self, name):
        d = self.hf / name / "snapshots" / "abc"
        d.mkdir(parents=True)
        (d / "w.bin").write_text("x")
        return self.hf / name

    def test_only_idle_user_dirs_removed(self):
        idle_dir = self._dir(f"user_{self.idle.pk}")
        online_dir = self._dir(f"user_{self.online.pk}")
        shared = {n: self._dir(n) for n in ("hf_home", "live", "user_abc", "user_5x", "user_")}
        Client().force_login(self.online)

        cleanup_orphaned_dirs()

        self.assertFalse(idle_dir.exists())
        self.assertTrue(online_dir.exists())
        self.assertTrue(self.hf.exists())
        for d in shared.values():
            self.assertTrue(d.exists(), d)

    def test_idle_users_hf_cache_removed_but_shared_kept(self):
        UserGameModel.objects.create(user=self.idle, game_type="chess", hf_model_repo_id="idle/model")
        UserGameModel.objects.create(user=self.online, game_type="chess", hf_model_repo_id="online/model")
        gone = self._repo("models--idle--model")
        kept = self._repo("models--online--model")
        self._dir(f"user_{self.idle.pk}")
        Client().force_login(self.online)

        cleanup_orphaned_dirs()

        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())

    def test_users_in_ongoing_games_are_skipped(self):
        UserGameModel.objects.create(user=self.player, game_type="chess", hf_model_repo_id="pl/model")
        d = self._dir(f"user_{self.player.pk}")
        repo = self._repo("models--pl--model")
        Game.objects.create(white=self.player, black=self.rival, status=Game.Status.ONGOING)

        cleanup_orphaned_dirs()

        self.assertTrue(d.exists())
        self.assertTrue(repo.exists())

    def test_waiting_game_with_both_seats_is_protected_but_open_table_is_not(self):
        both = self._dir(f"user_{self.player.pk}")
        open_table = self._dir(f"user_{self.idle.pk}")
        Game.objects.create(white=self.player, black=self.rival, status=Game.Status.WAITING)
        Game.objects.create(white=self.idle, black=None, status=Game.Status.WAITING)

        cleanup_orphaned_dirs()

        self.assertTrue(both.exists())
        self.assertFalse(open_table.exists())

    def test_hf_cache_delete_is_skipped_during_ongoing_game(self):
        UserGameModel.objects.create(user=self.player, game_type="chess", hf_model_repo_id="pl/model")
        repo = self._repo("models--pl--model")
        game = Game.objects.create(white=self.player, black=self.rival, status=Game.Status.ONGOING)

        self.assertEqual(delete_hf_cache_for_user(self.player.pk), [])
        self.assertTrue(repo.exists())

        Game.objects.filter(pk=game.pk).update(status=Game.Status.WHITE_WINS)
        self.assertEqual(len(delete_hf_cache_for_user(self.player.pk)), 1)
        self.assertFalse(repo.exists())

    def test_missing_base_dir_is_a_noop(self):
        with override_settings(USER_MODELS_BASE_DIR=self.base / "nope"):
            cleanup_orphaned_dirs()
