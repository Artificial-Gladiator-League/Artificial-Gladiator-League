"""Guest purge: 3 days after last activity, independent of cleanup_orphaned_dirs, never touches others."""
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.games.models import Game
from apps.users.guest_lifecycle import expired_guest_ids, purge_expired_guests
from apps.users.models import GuestUpgradeRequest, UserGameModel

User = get_user_model()
SHA = "a" * 40


class PurgeBase(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "um"
        self.hf = self.base / "hf_hub_cache"
        self.enterContext(override_settings(
            USER_MODELS_BASE_DIR=self.base, MODEL_CACHE_ROOT=self.base, HF_HUB_CACHE=str(self.hf),
            LIVE_MODELS_ROOT=self.base / "live",
        ))
        self.now = timezone.now()

    def guest(self, name, hf, expires, **extra):
        user = User.objects.create_user(
            name, password=None, is_guest=True, hf_sub=f"s-{name}", guest_hf_username=hf, ai_name=hf,
            guest_created_at=self.now - timezone.timedelta(days=10), guest_expires_at=expires, **extra,
        )
        UserGameModel.objects.create(user=user, game_type="chess", hf_model_repo_id=f"{hf}/m", approved_full_sha=SHA)
        folder = self.hf / f"models--{hf}--m" / "snapshots" / SHA
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "w.py").write_text("x")
        (self.base / f"user_{user.pk}" / "chess").mkdir(parents=True, exist_ok=True)
        return user

    def exists(self, user):
        return User.objects.filter(pk=user.pk).exists()


class PurgeTests(PurgeBase):
    def test_only_expired_guests_are_purged_with_their_files(self):
        old = self.guest("guest_pg000001", "oldg", self.now - timezone.timedelta(minutes=1))
        fresh = self.guest("guest_pg000002", "freshg", self.now + timezone.timedelta(days=2))
        registered = User.objects.create_user("regular", password="pw", guest_expires_at=self.now - timezone.timedelta(days=9))
        report = purge_expired_guests(self.now)
        self.assertEqual([(e["id"], e["action"]) for e in report], [(old.pk, "delete")])
        self.assertFalse(self.exists(old))
        self.assertFalse(UserGameModel.objects.filter(hf_model_repo_id="oldg/m").exists())
        self.assertFalse((self.hf / "models--oldg--m").exists())
        self.assertFalse((self.base / f"user_{old.pk}").exists())
        self.assertTrue(self.exists(fresh) and (self.hf / "models--freshg--m").exists())
        self.assertTrue(self.exists(registered))

    def test_three_day_window_comes_from_the_setting_and_activity_slides_it(self):
        guest = self.guest("guest_pg000003", "slide", self.now + timezone.timedelta(days=1))
        self.assertEqual(expired_guest_ids(self.now), [])
        self.assertEqual(expired_guest_ids(self.now + timezone.timedelta(days=1, minutes=1)), [guest.pk])

    def test_guest_without_expiry_falls_back_to_created_at(self):
        guest = self.guest("guest_pg000004", "noexp", None)
        User.objects.filter(pk=guest.pk).update(guest_created_at=self.now - timezone.timedelta(days=4))
        self.assertEqual(expired_guest_ids(self.now), [guest.pk])
        User.objects.filter(pk=guest.pk).update(guest_created_at=self.now - timezone.timedelta(days=1))
        self.assertEqual(expired_guest_ids(self.now), [])

    def test_dry_run_deletes_nothing(self):
        old = self.guest("guest_pg000005", "dry", self.now - timezone.timedelta(hours=1))
        report = purge_expired_guests(self.now, dry_run=True)
        self.assertEqual(report[0]["id"], old.pk)
        self.assertTrue(self.exists(old))
        self.assertTrue((self.hf / "models--dry--m").exists())

    def test_protected_platform_account_is_never_purged(self):
        bot = self.guest("guest_pg000006", "botacc", self.now - timezone.timedelta(days=9), is_official_bot=True)
        self.assertEqual(purge_expired_guests(self.now), [])
        self.assertTrue(self.exists(bot))

    def test_guest_in_an_ongoing_game_is_skipped_then_forced_after_a_day(self):
        a = self.guest("guest_pg000007", "ga", self.now - timezone.timedelta(hours=2))
        b = self.guest("guest_pg000008", "gb", self.now + timezone.timedelta(days=2))
        game = Game.objects.create(white=a, black=b, status=Game.Status.ONGOING, is_guest_game=True)
        report = purge_expired_guests(self.now)
        self.assertEqual(report[0]["action"], "skip (ongoing game)")
        self.assertTrue(self.exists(a))
        User.objects.filter(pk=a.pk).update(guest_expires_at=self.now - timezone.timedelta(days=1, minutes=5))
        report = purge_expired_guests(self.now)
        self.assertEqual(report[0]["action"], "abort games and delete")
        self.assertFalse(self.exists(a))
        self.assertTrue(self.exists(b))
        self.assertFalse(Game.objects.filter(pk=game.pk).exists())  # cascades with the guest's row

    def test_dry_run_does_not_abort_games(self):
        a = self.guest("guest_pg000009", "gc", self.now - timezone.timedelta(days=3))
        b = self.guest("guest_pg000010", "gd", self.now + timezone.timedelta(days=2))
        game = Game.objects.create(white=a, black=b, status=Game.Status.ONGOING, is_guest_game=True)
        purge_expired_guests(self.now, dry_run=True)
        self.assertEqual(Game.objects.get(pk=game.pk).status, Game.Status.ONGOING)

    def test_shared_cache_folder_of_another_account_survives(self):
        old = self.guest("guest_pg000011", "shared", self.now - timezone.timedelta(hours=1))
        other = User.objects.create_user("regular2", password="pw")
        UserGameModel.objects.create(user=other, game_type="breakthrough", hf_model_repo_id="shared/m")
        purge_expired_guests(self.now)
        self.assertFalse(self.exists(old))
        self.assertTrue((self.hf / "models--shared--m").exists())

    def test_stale_upgrade_requests_are_removed_too(self):
        user = User.objects.create_user("regular3", password="pw")
        for nonce, age in (("old", 8), ("recent", 1)):
            GuestUpgradeRequest.objects.create(
                user=user, nonce=nonce, username="u" + nonce, ai_name="a" + nonce, email=f"{nonce}@x.io", country="DE",
                password_hash="h", consented_at=self.now, expires_at=self.now - timezone.timedelta(days=age),
            )
        purge_expired_guests(self.now)
        self.assertEqual(list(GuestUpgradeRequest.objects.values_list("nonce", flat=True)), ["recent"])

    def test_does_not_depend_on_cleanup_orphaned_dirs(self):
        old = self.guest("guest_pg000012", "indep", self.now - timezone.timedelta(hours=1))
        with mock.patch("apps.users.model_lifecycle.cleanup_orphaned_dirs", side_effect=AssertionError("must not run")):
            purge_expired_guests(self.now)
        self.assertFalse(self.exists(old))

    def test_one_failure_does_not_stop_the_rest(self):
        first = self.guest("guest_pg000013", "f1", self.now - timezone.timedelta(hours=1))
        second = self.guest("guest_pg000014", "f2", self.now - timezone.timedelta(hours=1))
        real = __import__("apps.users.guest_lifecycle", fromlist=["x"]).delete_guest_account
        calls = []

        def flaky(user_id):
            calls.append(user_id)
            return False if user_id == first.pk else real(user_id)

        with mock.patch("apps.users.guest_lifecycle.delete_guest_account", side_effect=flaky):
            report = purge_expired_guests(self.now)
        self.assertEqual({e["id"]: e["action"] for e in report}, {first.pk: "failed", second.pk: "delete"})
        self.assertTrue(self.exists(first))
        self.assertFalse(self.exists(second))


class CommandAndScheduleTests(PurgeBase):
    def test_command_dry_run_and_real_run(self):
        old = self.guest("guest_pg000020", "cmd", self.now - timezone.timedelta(hours=1))
        out = StringIO()
        call_command("purge_guests", "--dry-run", stdout=out)
        self.assertIn(old.username, out.getvalue())
        self.assertIn("Would delete 1 of 1", out.getvalue())
        self.assertTrue(self.exists(old))
        out = StringIO()
        call_command("purge_guests", stdout=out)
        self.assertIn("Deleted 1 of 1", out.getvalue())
        self.assertFalse(self.exists(old))

    def test_celery_task_wrapper(self):
        from apps.users.tasks import purge_expired_guests_task

        self.guest("guest_pg000021", "tsk", self.now - timezone.timedelta(hours=1))
        self.assertEqual(purge_expired_guests_task(), 1)

    def test_beat_schedule_has_both_entries_on_the_guest_queue(self):
        from django.conf import settings

        schedule = settings.CELERY_BEAT_SCHEDULE
        self.assertEqual(schedule["purge-expired-guests"]["task"], "apps.users.tasks.purge_expired_guests_task")
        self.assertEqual(schedule["purge-expired-guests"]["schedule"], 3600.0)
        self.assertEqual(schedule["cleanup-idle-guest-snapshots"]["task"], "apps.users.tasks.cleanup_idle_guest_snapshots_task")
        for key in ("purge-expired-guests", "cleanup-idle-guest-snapshots"):
            self.assertEqual(schedule[key]["options"]["queue"], settings.GUEST_CELERY_QUEUE)
        self.assertEqual(settings.GUEST_CELERY_QUEUE, "celery")

    def test_scheduled_tasks_are_registered_with_celery(self):
        from agladiator.celery import app

        app.loader.import_default_modules()
        for name in ("apps.users.tasks.purge_expired_guests_task", "apps.users.tasks.cleanup_idle_guest_snapshots_task",
                     "apps.users.tasks.prepare_guest_snapshot_task"):
            self.assertIn(name, app.tasks)
