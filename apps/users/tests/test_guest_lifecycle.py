"""Guest snapshot and account clean-up: logout, idle, delete-my-data, shared folders, ongoing games."""
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.games.models import Game
from apps.users import guest_snapshot as gs
from apps.users.guest_lifecycle import (
    cleanup_idle_guest_snapshots,
    delete_guest_account,
    delete_guest_snapshots,
)
from apps.users.models import UserGameModel

User = get_user_model()
SHA_M, SHA_D = "a" * 40, "b" * 40
PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestCleanupBase(TestCase):
    def setUp(self):
        cache.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "um"
        self.hf = self.base / "hf_hub_cache"
        self.enterContext(override_settings(
            USER_MODELS_BASE_DIR=self.base, MODEL_CACHE_ROOT=self.base, HF_HUB_CACHE=str(self.hf),
            LIVE_MODELS_ROOT=self.base / "live",
        ))
        self.guest = self.make_guest("guest_life0001", "alice")

    def make_guest(self, name, hf, **extra):
        user = User.objects.create_user(
            name, password=None, is_guest=True, hf_sub=f"sub-{name}", guest_hf_username=hf, ai_name=hf,
            guest_created_at=timezone.now(), guest_last_seen=timezone.now(),
            guest_expires_at=timezone.now() + timezone.timedelta(days=3), **extra,
        )
        UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id=f"{hf}/m", hf_data_repo_id=f"{hf}/d",
            approved_full_sha=SHA_M, approved_data_repo_sha=SHA_D, status="active",
            cached_path=str(self.hf / f"models--{hf}--m" / "snapshots" / SHA_M), cached_commit=SHA_M,
            cached_at=timezone.now(), is_verified=True, verification_method="oauth_namespace",
        )
        self.make_files(user, hf)
        return user

    def make_files(self, user, hf):
        for folder in (f"models--{hf}--m", f"datasets--{hf}--d"):
            snap = self.hf / folder / "snapshots" / SHA_M
            snap.mkdir(parents=True, exist_ok=True)
            (snap / "w.py").write_text("x")
        cache_dir = self.base / f"user_{user.pk}" / "chess" / "data_cache" / f"datasets--{hf}--d" / SHA_D
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "d.npz").write_text("x")

    def model_dir(self, hf="alice"):
        return self.hf / f"models--{hf}--m"

    def data_dir(self, hf="alice"):
        return self.hf / f"datasets--{hf}--d"

    def user_dir(self, user=None):
        return self.base / f"user_{(user or self.guest).pk}"


class DeleteSnapshotsTests(GuestCleanupBase):
    def test_deletes_files_keeps_repo_ids_and_pins(self):
        self.assertTrue(delete_guest_snapshots(self.guest.pk))
        self.assertFalse(self.model_dir().exists())
        self.assertFalse(self.data_dir().exists())
        self.assertFalse(self.user_dir().exists())
        gm = UserGameModel.objects.get(user=self.guest)
        self.assertEqual((gm.hf_model_repo_id, gm.hf_data_repo_id), ("alice/m", "alice/d"))
        self.assertEqual((gm.approved_full_sha, gm.approved_data_repo_sha, gm.status), (SHA_M, SHA_D, "active"))
        self.assertEqual((gm.cached_path, gm.cached_commit, gm.cached_at), ("", "", None))

    def test_shared_cache_folder_used_by_another_account_is_kept(self):
        other = User.objects.create_user("registered", password="pw")
        UserGameModel.objects.create(user=other, game_type="breakthrough", hf_model_repo_id="alice/m")
        delete_guest_snapshots(self.guest.pk)
        self.assertTrue(self.model_dir().exists())
        self.assertFalse(self.data_dir().exists())
        self.assertFalse(self.user_dir().exists())

    def test_nothing_is_deleted_during_an_ongoing_game(self):
        rival = self.make_guest("guest_life0002", "bob")
        game = Game.objects.create(white=self.guest, black=rival, status=Game.Status.ONGOING, is_guest_game=True)
        self.assertFalse(delete_guest_snapshots(self.guest.pk))
        self.assertTrue(self.model_dir().exists() and self.user_dir().exists())
        self.assertNotEqual(UserGameModel.objects.get(user=self.guest).cached_path, "")
        Game.objects.filter(pk=game.pk).update(status=Game.Status.WHITE_WINS)
        self.assertTrue(delete_guest_snapshots(self.guest.pk))
        self.assertFalse(self.model_dir().exists())

    def test_waiting_game_with_both_seats_also_protects(self):
        rival = self.make_guest("guest_life0003", "carl")
        Game.objects.create(white=self.guest, black=rival, status=Game.Status.WAITING, is_guest_game=True)
        self.assertFalse(delete_guest_snapshots(self.guest.pk))
        self.assertTrue(self.model_dir().exists())

    def test_open_table_does_not_protect(self):
        Game.objects.create(white=self.guest, black=None, status=Game.Status.WAITING, is_guest_game=True)
        self.assertTrue(delete_guest_snapshots(self.guest.pk))

    def test_missing_files_are_fine_and_it_is_idempotent(self):
        self.assertTrue(delete_guest_snapshots(self.guest.pk))
        self.assertTrue(delete_guest_snapshots(self.guest.pk))

    def test_protected_accounts_are_never_touched(self):
        bot = self.make_guest("guest_life0004", "botacct", is_official_bot=True)
        self.assertFalse(delete_guest_snapshots(bot.pk))
        self.assertTrue(self.model_dir("botacct").exists())
        self.assertFalse(delete_guest_account(bot.pk))
        self.assertTrue(User.objects.filter(pk=bot.pk).exists())


class LogoutTests(GuestCleanupBase):
    def test_logout_deletes_snapshots_immediately(self):
        client = Client()
        client.force_login(self.guest)
        resp = client.post(reverse("users:logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.model_dir().exists())
        self.assertFalse(self.data_dir().exists())
        self.assertFalse(self.user_dir().exists())
        self.assertEqual(UserGameModel.objects.get(user=self.guest).approved_full_sha, SHA_M)

    def test_logout_during_a_game_leaves_everything_in_place(self):
        rival = self.make_guest("guest_life0005", "dana")
        Game.objects.create(white=self.guest, black=rival, status=Game.Status.ONGOING, is_guest_game=True)
        client = Client()
        client.force_login(self.guest)
        client.post(reverse("users:logout"))
        self.assertTrue(self.model_dir().exists())
        self.assertTrue(self.user_dir().exists())

    def test_registered_logout_is_unchanged(self):
        reg = User.objects.create_user("regular", password="pw")
        UserGameModel.objects.create(user=reg, game_type="chess", hf_model_repo_id="regular/m")
        folder = self.hf / "models--regular--m" / "snapshots" / SHA_M
        folder.mkdir(parents=True)
        (folder / "w.py").write_text("x")
        client = Client()
        client.force_login(reg)
        client.post(reverse("users:logout"))
        self.assertFalse((self.hf / "models--regular--m").exists())


class IdleCleanupTests(GuestCleanupBase):
    def test_only_idle_guests_with_files_are_cleaned(self):
        idle = self.make_guest("guest_life0010", "idle1")
        User.objects.filter(pk=idle.pk).update(guest_last_seen=timezone.now() - timezone.timedelta(minutes=121))
        busy = self.make_guest("guest_life0011", "busy1")
        User.objects.filter(pk=busy.pk).update(guest_last_seen=timezone.now() - timezone.timedelta(minutes=119))
        empty = self.make_guest("guest_life0012", "empty1")
        User.objects.filter(pk=empty.pk).update(guest_last_seen=timezone.now() - timezone.timedelta(hours=5))
        UserGameModel.objects.filter(user=empty).update(cached_path="")
        never = self.make_guest("guest_life0013", "never1")
        User.objects.filter(pk=never.pk).update(
            guest_last_seen=None, guest_created_at=timezone.now() - timezone.timedelta(hours=3),
        )
        registered = User.objects.create_user("regular2", password="pw")

        self.assertEqual(cleanup_idle_guest_snapshots(), 2)

        self.assertFalse(self.model_dir("idle1").exists())
        self.assertFalse(self.model_dir("never1").exists())
        self.assertTrue(self.model_dir("busy1").exists())
        self.assertTrue(self.model_dir("alice").exists())
        self.assertTrue(registered.pk)
        self.assertEqual(cleanup_idle_guest_snapshots(), 0)

    def test_idle_guest_in_a_game_is_left_alone(self):
        idle = self.make_guest("guest_life0014", "idle2")
        rival = self.make_guest("guest_life0015", "idle3")
        User.objects.filter(pk__in=[idle.pk, rival.pk]).update(guest_last_seen=timezone.now() - timezone.timedelta(hours=9))
        Game.objects.create(white=idle, black=rival, status=Game.Status.ONGOING, is_guest_game=True)
        cleanup_idle_guest_snapshots()
        self.assertTrue(self.model_dir("idle2").exists())

    @override_settings(GUEST_SNAPSHOT_IDLE_MINUTES=10)
    def test_idle_window_is_a_setting(self):
        User.objects.filter(pk=self.guest.pk).update(guest_last_seen=timezone.now() - timezone.timedelta(minutes=11))
        self.assertEqual(cleanup_idle_guest_snapshots(), 1)

    def test_celery_task_wrapper(self):
        from apps.users.tasks import cleanup_idle_guest_snapshots_task

        User.objects.filter(pk=self.guest.pk).update(guest_last_seen=timezone.now() - timezone.timedelta(hours=3))
        self.assertEqual(cleanup_idle_guest_snapshots_task(), 1)

    def test_returning_guest_requeues_pinned_downloads_at_most_once_a_minute(self):
        delete_guest_snapshots(self.guest.pk)
        self.guest = User.objects.get(pk=self.guest.pk)
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            gs.ensure_guest_snapshots(self.guest)
            gs.ensure_guest_snapshots(self.guest)
        sched.assert_called_once()

    def test_profile_visit_requeues_missing_snapshot(self):
        delete_guest_snapshots(self.guest.pk)
        client = Client()
        client.force_login(self.guest)
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            resp = client.get(reverse("users:profile") + "?tab=ai-models")
        self.assertEqual(resp.status_code, 200)
        sched.assert_called_once()


class DeleteDataViewTests(GuestCleanupBase):
    def test_guest_can_delete_everything_now(self):
        client = Client()
        client.force_login(self.guest)
        resp = client.post(reverse("users:guest_delete_data"))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.guest.pk).exists())
        self.assertFalse(UserGameModel.objects.filter(hf_model_repo_id="alice/m").exists())
        self.assertFalse(self.model_dir().exists())
        self.assertFalse(self.user_dir().exists())
        self.assertNotIn("_auth_user_id", client.session)

    def test_get_is_not_allowed(self):
        client = Client()
        client.force_login(self.guest)
        self.assertEqual(client.get(reverse("users:guest_delete_data")).status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.guest.pk).exists())

    def test_refused_during_an_ongoing_game(self):
        rival = self.make_guest("guest_life0020", "evan")
        Game.objects.create(white=self.guest, black=rival, status=Game.Status.ONGOING, is_guest_game=True)
        client = Client()
        client.force_login(self.guest)
        resp = client.post(reverse("users:guest_delete_data"))
        self.assertEqual(resp["Location"], reverse("games:lobby"))
        self.assertTrue(User.objects.filter(pk=self.guest.pk).exists())

    def test_registered_users_are_not_deleted_by_it(self):
        reg = User.objects.create_user("regular3", password="pw")
        client = Client()
        client.force_login(reg)
        resp = client.post(reverse("users:guest_delete_data"))
        self.assertEqual(resp["Location"], reverse("users:profile"))
        self.assertTrue(User.objects.filter(pk=reg.pk).exists())

    def test_anonymous_is_sent_to_login(self):
        resp = Client().post(reverse("users:guest_delete_data"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/users/login/", resp["Location"])

    def test_delete_account_ignores_registered_ids(self):
        reg = User.objects.create_user("regular4", password="pw")
        self.assertFalse(delete_guest_account(reg.pk))
        self.assertTrue(User.objects.filter(pk=reg.pk).exists())
