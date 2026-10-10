"""Guest preload: downloads start at login, the check runs after both downloads, files come back, failures are shown."""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.users import guest_snapshot as gs
from apps.users.guest_snapshot import RepoListing
from apps.users.models import UserGameModel
from apps.users.tests.test_guest_oauth import GuestOAuthBase

User = get_user_model()
SHA_M = "a" * 40
SHA_D = "b" * 40
FILES = (("chess_mcvs.py", 1000), ("config_model.json", 50))
PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)


def fake_listing(repo, kind, revision=None):
    return RepoListing(sha=revision or (SHA_D if kind == "dataset" else SHA_M), files=FILES)


def fake_download_model(repo_id, lst):
    snap = Path(settings.HF_HUB_CACHE) / ("models--" + repo_id.replace("/", "--")) / "snapshots" / lst.sha
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "chess_mcvs.py").write_text("def load(ctx): return {}\n")
    return snap


def fake_download_data(user_id, game_type, repo_id, lst):
    dest = gs.data_cache_dir(user_id, game_type, repo_id, lst.sha)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "zone.npz").write_text("x")
    return dest


class StorageMixin:
    def use_temp_storage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "um"
        self.enterContext(override_settings(
            USER_MODELS_BASE_DIR=root, MODEL_CACHE_ROOT=root, HF_HUB_CACHE=str(root / "hf_hub_cache"),
        ))
        self.root = root


class GuestLoginStartsDownloadsTests(StorageMixin, GuestOAuthBase):
    def setUp(self):
        super().setUp()
        self.use_temp_storage()

    def make_gm(self, user, **extra):
        return UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="alice/m", hf_data_repo_id="alice/d",
            is_verified=True, **extra,
        )

    def test_new_guest_login_has_nothing_to_download_and_does_not_fail(self):
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            _, resp = self.sign_in(sub="sub-new", name="alice")
        self.assertEqual(resp.status_code, 302)
        sched.assert_not_called()

    def test_returning_guest_without_a_pin_gets_the_latest_revision_and_a_check(self):
        self.sign_in(sub="sub-r1", name="alice")
        guest = User.objects.get(hf_sub="sub-r1")
        gm = self.make_gm(guest, status="failed", last_error="no zone-db .npz file found under data_dir or model_dir")
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            self.sign_in(sub="sub-r1", name="alice")
        sched.assert_called_once_with(gm.pk, None, None, pin=True)
        gm.refresh_from_db()
        self.assertEqual((gm.status, gm.last_error), ("pending", ""))

    def test_returning_guest_with_a_pin_gets_the_pinned_commits_back_without_a_new_check(self):
        self.sign_in(sub="sub-r2", name="alice")
        guest = User.objects.get(hf_sub="sub-r2")
        gm = self.make_gm(guest, status="active", approved_full_sha=SHA_M, approved_data_repo_sha=SHA_D)
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            self.sign_in(sub="sub-r2", name="alice")
        sched.assert_called_once_with(gm.pk, SHA_M, SHA_D, pin=False)

    def test_returning_guest_gets_both_folders_back_and_is_ready(self):
        self.sign_in(sub="sub-r3", name="alice")
        guest = User.objects.get(hf_sub="sub-r3")
        gm = self.make_gm(guest, status="active", approved_full_sha=SHA_M, approved_data_repo_sha=SHA_D)
        fake_download_model("alice/m", fake_listing("alice/m", "model", SHA_M))
        fake_download_data(guest.pk, "chess", "alice/d", fake_listing("alice/d", "dataset", SHA_D))
        self.assertTrue(gs.guest_snapshot_ready(guest, "chess")[0])

        shutil.rmtree(self.root)  # what the logout clean-up leaves behind
        self.assertFalse(gs.guest_snapshot_ready(guest, "chess")[0])

        def run_now(gm_id, model_sha=None, data_sha=None, *, pin=True):
            gs.prepare_guest_snapshot(gm_id, model_sha, data_sha, pin=pin)

        with mock.patch.object(gs, "schedule_guest_prepare", side_effect=run_now), \
                mock.patch.object(gs, "fetch_listing", side_effect=fake_listing), \
                mock.patch.object(gs, "download_model", side_effect=fake_download_model), \
                mock.patch.object(gs, "download_data", side_effect=fake_download_data):
            self.sign_in(sub="sub-r3", name="alice")

        self.assertTrue(any((Path(settings.HF_HUB_CACHE) / "models--alice--m" / "snapshots" / SHA_M).iterdir()))
        self.assertTrue(any(gs.data_cache_dir(guest.pk, "chess", "alice/d", SHA_D).iterdir()))
        self.assertEqual(gs.guest_snapshot_ready(guest, "chess"), (True, "OK"))
        gm.refresh_from_db()
        self.assertEqual((gm.status, gm.prepare_stage), ("active", ""))


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestPrepareOrderTests(StorageMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.use_temp_storage()
        self.guest = User.objects.create_user(
            "guest_pre00001", password=None, is_guest=True, hf_sub="sub-pre1", guest_hf_username="alice",
            ai_name="alice", guest_expires_at=timezone.now() + timezone.timedelta(days=3),
        )
        self.gm = UserGameModel.objects.create(
            user=self.guest, game_type="chess", hf_model_repo_id="alice/m", hf_data_repo_id="alice/d",
            is_verified=True,
        )
        self.client = Client()
        self.client.force_login(self.guest)

    def stage(self):
        return UserGameModel.objects.get(pk=self.gm.pk).prepare_stage

    def test_check_runs_only_after_both_downloads_and_stages_are_recorded(self):
        events = []

        def model(repo_id, lst):
            events.append(("model", self.stage()))
            return fake_download_model(repo_id, lst)

        def data(user_id, game_type, repo_id, lst):
            events.append(("data", self.stage()))
            return fake_download_data(user_id, game_type, repo_id, lst)

        def check(gm, token, **kwargs):
            events.append(("check", self.stage()))
            UserGameModel.objects.filter(pk=gm.pk).update(status="active", approved_full_sha=SHA_M)

        with mock.patch.object(gs, "fetch_listing", side_effect=fake_listing), \
                mock.patch.object(gs, "download_model", side_effect=model), \
                mock.patch.object(gs, "download_data", side_effect=data), \
                mock.patch("apps.users.integrity.record_original_sha", side_effect=check):
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)

        self.assertEqual(events, [
            ("model", "downloading_model"), ("data", "downloading_data"), ("check", "checking"),
        ])
        self.assertEqual(self.stage(), "")

    def test_latest_revision_is_downloaded_when_nothing_is_pinned(self):
        with mock.patch.object(gs, "fetch_listing", side_effect=fake_listing) as fetch, \
                mock.patch.object(gs, "download_model", side_effect=fake_download_model), \
                mock.patch.object(gs, "download_data", side_effect=fake_download_data), \
                mock.patch("apps.users.integrity.record_original_sha") as rec:
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        self.assertTrue(all(c.kwargs.get("revision") is None for c in fetch.call_args_list))
        self.assertEqual(rec.call_args.kwargs, {"sha": SHA_M, "data_sha": SHA_D})

    def test_check_does_not_run_when_the_data_download_fails(self):
        with mock.patch.object(gs, "fetch_listing", side_effect=fake_listing), \
                mock.patch.object(gs, "download_model", side_effect=fake_download_model), \
                mock.patch.object(gs, "download_data", side_effect=OSError("disk full")), \
                mock.patch("apps.users.integrity.record_original_sha") as rec:
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        rec.assert_not_called()
        gm = UserGameModel.objects.get(pk=self.gm.pk)
        self.assertEqual(gm.status, "failed")
        self.assertIn("Downloading the data repo failed", gm.last_error)
        self.assertIn("disk full", gm.last_error)

    def test_save_during_a_running_preparation_runs_again_afterwards(self):
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            cache.add(f"guest-prepare:{self.gm.pk}", 1, timeout=60)
            gs.prepare_guest_snapshot(self.gm.pk, SHA_M, SHA_D, pin=True)  # dropped while locked, remembered
            cache.delete(f"guest-prepare:{self.gm.pk}")
            with mock.patch.object(gs, "fetch_listing", side_effect=fake_listing), \
                    mock.patch.object(gs, "download_model", side_effect=fake_download_model), \
                    mock.patch.object(gs, "download_data", side_effect=fake_download_data), \
                    mock.patch("apps.users.integrity.record_original_sha"):
                gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        sched.assert_called_once_with(self.gm.pk, SHA_M, SHA_D, pin=True)


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestFailureShownTests(StorageMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.use_temp_storage()
        self.guest = User.objects.create_user(
            "guest_pre00002", password=None, is_guest=True, hf_sub="sub-pre2", guest_hf_username="alice",
            ai_name="alice", guest_expires_at=timezone.now() + timezone.timedelta(days=3),
        )
        self.gm = UserGameModel.objects.create(
            user=self.guest, game_type="chess", hf_model_repo_id="alice/m", is_verified=True,
        )
        self.client = Client()
        self.client.force_login(self.guest)
        patcher = mock.patch("apps.users.guest_snapshot.ensure_guest_snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self):
        return self.client.get(reverse("users:guest_model_status", args=["chess"])).json()

    def test_model_download_error_is_stored_returned_and_rendered(self):
        with mock.patch.object(gs, "fetch_listing", side_effect=fake_listing), \
                mock.patch.object(gs, "download_model", side_effect=RuntimeError("connection reset")):
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        state = self.status()
        self.assertEqual(state["stage"], "failed")
        self.assertFalse(state["busy"])
        self.assertIn("Downloading the model repo failed", state["error"])
        self.assertIn("connection reset", state["error"])
        html = self.client.get(reverse("users:profile")).content.decode()
        self.assertIn("connection reset", html)
        self.assertIn("Model check failed", html)

    def test_running_stage_is_reported_and_polled(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(prepare_stage="downloading_data")
        cache.add(f"guest-prepare:{self.gm.pk}", 1, timeout=60)
        state = self.status()
        self.assertEqual((state["stage"], state["busy"]), ("downloading_data", True))
        self.assertIn("data repo", state["label"])
        html = self.client.get(reverse("users:profile")).content.decode()
        self.assertIn('data-busy="1"', html)
        self.assertIn(reverse("users:guest_model_status", args=["chess"]), html)

    def test_game_start_is_blocked_while_downloads_run(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(
            status="active", approved_full_sha=SHA_M, prepare_stage="downloading_model",
        )
        cache.add(f"guest-prepare:{self.gm.pk}", 1, timeout=60)
        ready, reason = gs.guest_snapshot_ready(self.guest, "chess")
        self.assertFalse(ready)
        self.assertIn("Downloading the model repo", reason)

    def test_status_endpoint_is_for_guests_only(self):
        user = User.objects.create_user("regular_pre", password="pw")
        c = Client()
        c.force_login(user)
        self.assertEqual(c.get(reverse("users:guest_model_status", args=["chess"])).status_code, 403)
