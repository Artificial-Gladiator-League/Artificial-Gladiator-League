"""Guest repo connection: public repos, namespace proof, size limits, pinned anonymous download."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.users import guest_snapshot as gs
from apps.users.guest_snapshot import GuestRepoError, RepoListing
from apps.users.guided_steps import build_steps, next_step_line
from apps.users.models import UserGameModel

User = get_user_model()
SHA_M = "a" * 40
SHA_D = "b" * 40
PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)


def listing(sha=SHA_M, files=(("chess_mcvs.py", 1000), ("config_model.json", 50))):
    return RepoListing(sha=sha, files=tuple(files))


def make_guest(name="guest_repo0001", hf="alice"):
    return User.objects.create_user(
        name, password=None, is_guest=True, hf_sub=f"sub-{name}", guest_hf_username=hf,
        ai_name=hf, guest_expires_at=timezone.now() + timezone.timedelta(days=3),
    )


def msgs(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


@PLAIN_STATIC
@override_settings(GUEST_MODE_ENABLED=True)
class GuestConnectTests(TestCase):
    def setUp(self):
        cache.clear()
        self.guest = make_guest()
        self.client = Client()
        self.client.force_login(self.guest)
        patcher = mock.patch("apps.users.guest_repos.schedule_guest_prepare")
        self.schedule = patcher.start()
        self.addCleanup(patcher.stop)
        fetch = mock.patch(
            "apps.users.guest_repos.fetch_listing",
            side_effect=lambda repo, kind, revision=None: listing(SHA_D if kind == "dataset" else SHA_M),
        )
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def post(self, model="alice/chess-model", data="", game="chess", action="connect"):
        return self.client.post(reverse("users:profile"), {
            "ai_model_form": "1", "game_type": game, "action": action,
            "hf_model_repo_id": model, "hf_data_repo_id": data,
        })

    def test_connect_saves_public_repos_without_gated_or_agl_checks(self):
        with mock.patch("apps.users.views._check_repo_is_gated", side_effect=AssertionError("gated check")), \
                mock.patch("apps.users.ownership_verification.check_full_ownership", side_effect=AssertionError("agl")), \
                mock.patch("apps.users.views.generate_verification_code", create=True, side_effect=AssertionError("code")):
            resp = self.post(data="alice/chess-data")
        self.assertEqual(resp.status_code, 302)
        gm = self.guest.get_game_model("chess")
        self.assertEqual((gm.hf_model_repo_id, gm.hf_data_repo_id), ("alice/chess-model", "alice/chess-data"))
        self.assertEqual(gm.verification_method, "oauth_namespace")
        self.assertTrue(gm.is_verified and gm.model_repo_ownership_verified and gm.data_repo_ownership_verified)
        self.assertEqual(gm.verification_code, "")
        self.assertEqual(gm.status, "pending")
        self.schedule.assert_called_once_with(gm.pk, SHA_M, SHA_D, pin=True)

    def test_url_style_ids_are_normalised(self):
        self.post(model="https://huggingface.co/alice/chess-model")
        self.assertEqual(self.guest.get_game_model("chess").hf_model_repo_id, "alice/chess-model")

    def test_rejected_inputs_save_nothing(self):
        bad = [
            ("bob/chess-model", ""),           # not his namespace
            ("Alice/chess-model", "bob/data"),  # data repo in someone else's namespace
            ("alice/chess--model", ""),        # cache folder separator
            ("alice/../x", ""),
            ("alice", ""),
            ("alice/a b", ""),
        ]
        for model, data in bad:
            resp = self.post(model=model, data=data)
            self.assertTrue(msgs(resp), (model, data))
        self.assertFalse(UserGameModel.objects.filter(user=self.guest).exists())
        self.schedule.assert_not_called()

    def test_namespace_check_is_case_insensitive(self):
        self.post(model="ALICE/chess-model")
        self.assertEqual(self.guest.get_game_model("chess").hf_model_repo_id, "ALICE/chess-model")

    def test_gated_or_private_repo_is_refused_with_the_reason(self):
        self.fetch.side_effect = GuestRepoError("The model repo 'alice/chess-model' is gated. Guests need a public repo.")
        resp = self.post()
        self.assertTrue(any("gated" in m for m in msgs(resp)))
        self.assertFalse(UserGameModel.objects.filter(user=self.guest).exists())

    @override_settings(GUEST_MAX_REPO_MB=1)
    def test_size_limit_is_checked_before_any_download(self):
        self.fetch.side_effect = lambda repo, kind, revision=None: listing(files=(("w.safetensors", 2 * 1024 * 1024),))
        with mock.patch("apps.users.guest_snapshot.download_model") as dl:
            resp = self.post()
        self.assertTrue(any("at most 1 MB" in m for m in msgs(resp)))
        dl.assert_not_called()
        self.assertFalse(UserGameModel.objects.filter(user=self.guest).exists())

    @override_settings(GUEST_MAX_REPO_FILES=2)
    def test_file_count_limit(self):
        self.fetch.side_effect = lambda repo, kind, revision=None: listing(files=(("a", 1), ("b", 1), ("c", 1)))
        resp = self.post()
        self.assertTrue(any("at most 2" in m for m in msgs(resp)))

    def test_repo_used_by_another_account_is_refused(self):
        other = User.objects.create_user("owner", password="pw")
        UserGameModel.objects.create(user=other, game_type="chess", hf_model_repo_id="Alice/Chess-Model")
        resp = self.post()
        self.assertTrue(any("already registered" in m for m in msgs(resp)))
        self.assertFalse(UserGameModel.objects.filter(user=self.guest).exists())

    def test_legacy_registered_repo_is_refused(self):
        User.objects.create_user("legacy", password="pw", hf_model_repo_id="alice/chess-model")
        resp = self.post()
        self.assertTrue(any("already registered" in m for m in msgs(resp)))

    @override_settings(GUEST_RATE_CONNECT_PER_USER="1/h")
    def test_connect_attempts_are_rate_limited(self):
        self.post()
        resp = self.post(model="alice/other")
        self.assertTrue(any("Too many" in m for m in msgs(resp)))
        self.assertEqual(self.schedule.call_count, 1)

    def test_changing_the_repo_resets_pins_but_resaving_the_same_repo_keeps_them(self):
        self.post()
        UserGameModel.objects.filter(user=self.guest).update(
            approved_full_sha=SHA_M, cached_commit=SHA_M, approved_data_repo_sha=SHA_D,
        )
        self.post()
        self.assertEqual(self.guest.get_game_model("chess").approved_full_sha, SHA_M)
        self.post(model="alice/new-model")
        gm = self.guest.get_game_model("chess")
        self.assertEqual((gm.approved_full_sha, gm.cached_commit, gm.approved_data_repo_sha), ("", "", ""))

    def test_verify_action_requeues_the_check(self):
        self.post()
        self.schedule.reset_mock()
        UserGameModel.objects.filter(user=self.guest).update(status="failed", last_error="boom")
        self.post(action="verify")
        gm = self.guest.get_game_model("chess")
        self.assertEqual((gm.status, gm.last_error), ("pending", ""))
        self.schedule.assert_called_once_with(gm.pk, None, None, pin=True)

    def test_registered_users_keep_the_gated_flow(self):
        user = User.objects.create_user("regular", password="pw")
        c = Client()
        c.force_login(user)
        with mock.patch("apps.users.guest_repos.handle_guest_post") as handler, \
                mock.patch("apps.users.views._check_repo_is_gated", return_value="needs gating") as gated:
            resp = c.post(reverse("users:profile"), {
                "ai_model_form": "1", "game_type": "chess", "action": "connect",
                "hf_model_repo_id": "regular/model",
            })
        handler.assert_not_called()
        gated.assert_called_once()
        self.assertIn("needs gating", msgs(resp))


class FetchListingTests(SimpleTestCase):
    def _api(self, head, info):
        api = mock.MagicMock()
        api.repo_info.side_effect = [head, info]
        return mock.MagicMock(return_value=api), api

    def test_public_repo_listing_is_anonymous_and_pinned(self):
        head = SimpleNamespace(sha=SHA_M, gated=False, private=False)
        info = SimpleNamespace(sha=SHA_M, siblings=[SimpleNamespace(rfilename="a.py", size=10)])
        cls, api = self._api(head, info)
        with mock.patch("huggingface_hub.HfApi", cls):
            result = gs.fetch_listing("alice/m")
        self.assertEqual(result, RepoListing(SHA_M, (("a.py", 10),)))
        first, second = api.repo_info.call_args_list
        self.assertIs(first.kwargs["token"], False)
        self.assertIs(second.kwargs["token"], False)
        self.assertEqual(second.kwargs["revision"], SHA_M)
        self.assertTrue(second.kwargs["files_metadata"])

    def test_gated_and_private_are_refused(self):
        for flag in ("gated", "private"):
            head = SimpleNamespace(sha=SHA_M, gated=flag == "gated" and "auto", private=flag == "private")
            cls, _ = self._api(head, None)
            with mock.patch("huggingface_hub.HfApi", cls):
                with self.assertRaises(GuestRepoError) as ctx:
                    gs.fetch_listing("alice/m")
            self.assertIn("public", str(ctx.exception))

    def test_unknown_size_or_unsafe_name_is_refused(self):
        head = SimpleNamespace(sha=SHA_M, gated=False, private=False)
        for sibling in (SimpleNamespace(rfilename="a.py", size=None), SimpleNamespace(rfilename="../evil", size=1)):
            cls, _ = self._api(head, SimpleNamespace(sha=SHA_M, siblings=[sibling]))
            with mock.patch("huggingface_hub.HfApi", cls):
                with self.assertRaises(GuestRepoError):
                    gs.fetch_listing("alice/m")

    def test_pinned_revision_skips_head_lookup(self):
        info = SimpleNamespace(sha=SHA_M, siblings=[SimpleNamespace(rfilename="a.py", size=1)])
        api = mock.MagicMock()
        api.repo_info.return_value = info
        with mock.patch("huggingface_hub.HfApi", mock.MagicMock(return_value=api)):
            gs.fetch_listing("alice/m", revision=SHA_M)
        self.assertEqual(api.repo_info.call_count, 1)


class DownloadTests(SimpleTestCase):
    def test_model_download_is_pinned_anonymous_and_restricted_to_checked_files(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(HF_HUB_CACHE=tmp):
            snap = Path(tmp) / "models--alice--m" / "snapshots" / SHA_M
            snap.mkdir(parents=True)
            with mock.patch("huggingface_hub.snapshot_download") as dl:
                result = gs.download_model("alice/m", listing(files=(("w[1].safetensors", 5), ("a.py", 1))))
        kwargs = dl.call_args.kwargs
        self.assertIs(kwargs["token"], False)
        self.assertEqual(kwargs["revision"], SHA_M)
        self.assertEqual(kwargs["cache_dir"], tmp)
        self.assertEqual(kwargs["allow_patterns"], ["w[[]1].safetensors", "a.py"])
        self.assertEqual(result.name, SHA_M)

    def test_data_download_goes_to_the_pinned_revision_folder(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(USER_MODELS_BASE_DIR=Path(tmp)):
            with mock.patch("huggingface_hub.snapshot_download") as dl:
                dest = gs.download_data(7, "chess", "alice/d", listing(SHA_D))
            self.assertEqual(dest, Path(tmp) / "user_7" / "chess" / "data_cache" / "datasets--alice--d" / SHA_D)
        self.assertEqual(dl.call_args.kwargs["revision"], SHA_D)
        self.assertIs(dl.call_args.kwargs["token"], False)


@override_settings(GUEST_MODE_ENABLED=True)
class PrepareTests(TestCase):
    def setUp(self):
        cache.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.enterContext(override_settings(
            USER_MODELS_BASE_DIR=self.tmp / "um", MODEL_CACHE_ROOT=self.tmp / "um",
            HF_HUB_CACHE=str(self.tmp / "um" / "hf_hub_cache"),
        ))
        self.guest = make_guest("guest_prep0001")
        self.gm = UserGameModel.objects.create(
            user=self.guest, game_type="chess", hf_model_repo_id="alice/m", hf_data_repo_id="alice/d",
            is_verified=True, verification_method="oauth_namespace",
        )
        self.snap = self.tmp / "snap"
        self.snap.mkdir()
        (self.snap / "chess_mcvs.py").write_text("def load(ctx): return {}\n")
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "d.npz").write_text("x")

    def _patches(self, check=None):
        return (
            mock.patch.object(gs, "fetch_listing", side_effect=lambda r, k, revision=None: listing(SHA_D if k == "dataset" else SHA_M)),
            mock.patch.object(gs, "download_model", return_value=self.snap),
            mock.patch.object(gs, "download_data", return_value=self.data),
            mock.patch("apps.games.local_inference.resolve_model_path", return_value=(self.snap, self.data)),
            mock.patch("apps.games.model_check.check_model", return_value=check or []),
        )

    def test_pipeline_pins_both_shas_and_activates(self):
        fetch, dl_m, dl_d, resolve, check = self._patches()
        with fetch, dl_m, dl_d as dl_data, resolve as res, check as chk:
            gs.prepare_guest_snapshot(self.gm.pk, SHA_M, SHA_D, pin=True)
        gm = UserGameModel.objects.get(pk=self.gm.pk)
        self.assertEqual(gm.status, "active")
        self.assertEqual((gm.approved_full_sha, gm.approved_data_repo_sha), (SHA_M, SHA_D))
        self.assertEqual((gm.cached_commit, gm.cached_path), (SHA_M, str(self.snap)))
        self.assertEqual(gm.verification_method, "oauth_namespace")
        self.assertEqual(res.call_args.kwargs["data_revision"], SHA_D)
        chk.assert_called_once()

    def test_failed_model_check_is_shown_to_the_guest(self):
        fetch, dl_m, dl_d, resolve, check = self._patches(check=["entrypoint is missing get_move()"])
        with fetch, dl_m, dl_d, resolve, check:
            gs.prepare_guest_snapshot(self.gm.pk, SHA_M, SHA_D, pin=True)
        gm = UserGameModel.objects.get(pk=self.gm.pk)
        self.assertEqual(gm.status, "failed")
        self.assertIn("get_move()", gm.last_error)
        self.assertEqual(gm.approved_full_sha, "")

    def test_repo_problem_marks_failed_and_downloads_nothing(self):
        with mock.patch.object(gs, "fetch_listing", side_effect=GuestRepoError("It is private. Your repos must be public.")), \
                mock.patch.object(gs, "download_model") as dl:
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        gm = UserGameModel.objects.get(pk=self.gm.pk)
        self.assertEqual(gm.status, "failed")
        self.assertIn("must be public", gm.last_error)
        dl.assert_not_called()

    def test_redownload_of_pinned_commit_does_not_repin_or_change_status(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(
            status="active", approved_full_sha=SHA_M, approved_data_repo_sha=SHA_D,
        )
        fetch, dl_m, dl_d, resolve, check = self._patches()
        with fetch as f, dl_m, dl_d, resolve, check as chk, mock.patch("apps.users.integrity.record_original_sha") as rec:
            gs.prepare_guest_snapshot(self.gm.pk, SHA_M, SHA_D, pin=False)
        rec.assert_not_called()
        chk.assert_not_called()
        self.assertEqual(f.call_args_list[0].kwargs["revision"], SHA_M)
        gm = UserGameModel.objects.get(pk=self.gm.pk)
        self.assertEqual((gm.status, gm.approved_full_sha, gm.cached_commit), ("active", SHA_M, SHA_M))

    def test_unexpected_error_is_reported_without_failing_the_pin(self):
        with mock.patch.object(gs, "fetch_listing", side_effect=RuntimeError("boom")):
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        gm = UserGameModel.objects.get(pk=self.gm.pk)
        self.assertEqual(gm.status, "pending")
        self.assertIn("try again", gm.last_error)

    def test_non_guest_rows_are_ignored(self):
        user = User.objects.create_user("plain", password="pw")
        gm = UserGameModel.objects.create(user=user, game_type="chess", hf_model_repo_id="plain/m")
        with mock.patch.object(gs, "fetch_listing") as fetch:
            gs.prepare_guest_snapshot(gm.pk, None, None, pin=True)
        fetch.assert_not_called()

    def test_ready_check_requires_active_pinned_files_and_data(self):
        ready, reason = gs.guest_snapshot_ready(self.guest, "chess")
        self.assertEqual((ready, reason), (False, "Preparing your model..."))
        hf = Path(self.tmp / "um" / "hf_hub_cache" / "models--alice--m" / "snapshots" / SHA_M)
        hf.mkdir(parents=True)
        (hf / "chess_mcvs.py").write_text("def load(ctx): return {}\n")
        UserGameModel.objects.filter(pk=self.gm.pk).update(status="active", approved_full_sha=SHA_M)
        self.guest = User.objects.get(pk=self.guest.pk)
        self.assertFalse(gs.guest_snapshot_ready(self.guest, "chess")[0])  # data repo not downloaded yet
        data_dir = gs.data_cache_dir(self.guest.pk, "chess", "alice/d", SHA_D)
        data_dir.mkdir(parents=True)
        (data_dir / "d.npz").write_text("x")
        UserGameModel.objects.filter(pk=self.gm.pk).update(approved_data_repo_sha=SHA_D)
        self.assertEqual(gs.guest_snapshot_ready(self.guest, "chess"), (True, "OK"))
        UserGameModel.objects.filter(pk=self.gm.pk).update(status="failed", last_error="bad model")
        self.assertEqual(gs.guest_snapshot_ready(self.guest, "chess"), (False, "bad model"))

    def test_login_requeues_only_missing_pinned_snapshots(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(
            status="active", approved_full_sha=SHA_M, approved_data_repo_sha=SHA_D,
        )
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            gs.prepare_pinned_snapshots(self.guest)
        sched.assert_called_once_with(self.gm.pk, SHA_M, SHA_D, pin=False)
        UserGameModel.objects.filter(pk=self.gm.pk).update(status="failed")
        with mock.patch.object(gs, "schedule_guest_prepare") as sched:
            gs.prepare_pinned_snapshots(self.guest)
        sched.assert_not_called()

    def test_concurrent_preparation_is_skipped(self):
        cache.add(f"guest-prepare:{self.gm.pk}", 1, timeout=60)
        with mock.patch.object(gs, "fetch_listing") as fetch:
            gs.prepare_guest_snapshot(self.gm.pk, None, None, pin=True)
        fetch.assert_not_called()


class GuestNeverUsesUnrestrictedDownloadsTests(TestCase):
    def test_ensure_hf_snapshot_does_not_download_for_guests(self):
        from apps.games.model_preloader import ensure_hf_snapshot

        guest = make_guest("guest_nodl0001")
        gm = UserGameModel.objects.create(user=guest, game_type="chess", hf_model_repo_id="alice/m")
        with mock.patch("huggingface_hub.snapshot_download", side_effect=AssertionError("download")):
            self.assertIsNone(ensure_hf_snapshot(gm))

    def test_move_time_resolution_never_fetches_guest_data_or_uses_a_token(self):
        from apps.games import local_inference as li

        guest = make_guest("guest_nodl0002")
        UserGameModel.objects.create(
            user=guest, game_type="chess", hf_model_repo_id="alice/m", hf_data_repo_id="alice/d",
            approved_full_sha=SHA_M, approved_data_repo_sha=SHA_D,
        )
        with mock.patch.object(li, "resolve_model_path", return_value=(None, None)) as res:
            li._resolve_for_sandbox(guest.pk, "chess", "alice/m")
        self.assertIsNone(res.call_args.kwargs["data_repo_id"])
        self.assertIs(res.call_args.kwargs["data_token"], False)

    def test_registered_users_resolution_is_unchanged(self):
        from apps.games import local_inference as li

        user = User.objects.create_user("plain2", password="pw")
        UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="plain2/m", hf_data_repo_id="plain2/d",
        )
        with mock.patch.object(li, "resolve_model_path", return_value=(None, None)) as res:
            li._resolve_for_sandbox(user.pk, "chess", "plain2/m")
        self.assertEqual(res.call_args.kwargs["data_repo_id"], "plain2/d")


class GuidedStepsTests(TestCase):
    def test_guest_gets_two_public_repo_steps(self):
        guest = make_guest("guest_step0001")
        self.assertEqual([s["title"] for s in build_steps(None, guest=True)],
                         ["Connect your public repos", "Platform model check"])
        gm = UserGameModel.objects.create(user=guest, game_type="chess", hf_model_repo_id="alice/m")
        steps = build_steps(gm)
        self.assertEqual([s["state"] for s in steps], ["done", "current"])
        cfg = [{"label": "Chess", "model": gm, "steps": steps}]
        line = next_step_line(cfg)
        self.assertNotIn("AGL_VERIFY", line)
        self.assertNotIn("ArtificialGladiatorLeague", line)
        self.assertIn("Preparing your model", line)
        empty = [{"label": "Chess", "model": None, "steps": build_steps(None, guest=True)}]
        self.assertIn("must be public", next_step_line(empty, guest=True))

    def test_upgraded_account_sees_all_four_steps_unfinished(self):
        user = User.objects.create_user("upgraded2", password="pw")
        gm = UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="upgraded2/m", is_verified=True,
            verification_method="oauth_namespace", model_repo_ownership_verified=True, status="active",
        )
        steps = build_steps(gm)
        self.assertEqual(len(steps), 4)
        self.assertEqual([s["state"] for s in steps], ["done", "current", "todo", "todo"])

    def test_file_verified_account_is_unchanged(self):
        user = User.objects.create_user("filev2", password="pw")
        gm = UserGameModel.objects.create(
            user=user, game_type="chess", hf_model_repo_id="filev2/m", is_verified=True, status="active",
        )
        self.assertEqual([s["state"] for s in build_steps(gm)], ["done"] * 4)
