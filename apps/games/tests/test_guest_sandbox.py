"""Guest sandbox profile: stricter limits, capped IPC reads, guest worker cap, no eviction of registered workers."""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.games import sandbox_runner as sr
from apps.games.exceptions import SandboxUnavailableError
from apps.users.models import DiagnosticRun, UserGameModel

User = get_user_model()


class FakeContainer:
    def __init__(self, on_start=None, logs=b'{"move": "e2e4"}\n', status_code=0):
        self.on_start, self._logs, self.status_code = on_start, logs, status_code
        self.status = "running"
        self.wait_timeout = None
        self.removed = self.killed = False

    def start(self):
        if self.on_start:
            self.on_start()

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        return {"StatusCode": self.status_code}

    def logs(self, stdout=True, stderr=True):
        return self._logs

    def reload(self):
        pass

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class FakeClient:
    def __init__(self, make_container):
        self.created = []
        self._make = make_container
        self.containers = self

    def ping(self):
        return True

    def create(self, **kwargs):
        self.created.append(kwargs)
        return self._make(kwargs)


def model_dir_with_file(tmp):
    d = Path(tmp) / "model"
    d.mkdir()
    (d / "chess_mcvs.py").write_text("def load(ctx): return {}\n")
    return d


class LimitsTests(SimpleTestCase):
    def test_registered_limits_are_exactly_the_existing_ones(self):
        self.assertEqual(sr._limits(None), {"mem_limit": "512m", "nano_cpus": 1_000_000_000, "pids_limit": 128})

    @override_settings(SANDBOX_MEMORY_LIMIT_MB=640, SANDBOX_CPU_LIMIT=2.0, SANDBOX_PIDS_LIMIT=200)
    def test_registered_limits_follow_the_registered_settings_only(self):
        self.assertEqual(sr._limits(None), {"mem_limit": "640m", "nano_cpus": 2_000_000_000, "pids_limit": 200})

    def test_guest_profile_is_stricter(self):
        limits = sr._limits("guest")
        self.assertEqual(limits["mem_limit"], "256m")
        self.assertEqual(limits["memswap_limit"], limits["mem_limit"])
        self.assertEqual(limits["nano_cpus"], 500_000_000)
        self.assertEqual(limits["pids_limit"], 64)
        by_name = {u["Name"]: u for u in (u for u in limits["ulimits"])} if isinstance(limits["ulimits"][0], dict) else \
            {u.name: u for u in limits["ulimits"]}
        self.assertEqual(set(by_name), {"nofile", "fsize", "core"})
        fsize = by_name["fsize"]
        self.assertEqual(getattr(fsize, "hard", None) or fsize["Hard"], 8 * 1024 * 1024)
        self.assertLess(limits["nano_cpus"], sr._limits(None)["nano_cpus"])
        self.assertLess(256, 512)

    @override_settings(SANDBOX_GUEST_MEMORY_LIMIT_MB=128, SANDBOX_GUEST_PIDS_LIMIT=32, SANDBOX_GUEST_FSIZE_MB=2)
    def test_guest_limits_are_settings(self):
        limits = sr._limits("guest")
        self.assertEqual((limits["mem_limit"], limits["memswap_limit"], limits["pids_limit"]), ("128m", "128m", 32))

    def test_runtime_is_unset_by_default_and_applies_when_set(self):
        self.assertNotIn("runtime", sr._limits(None))
        self.assertNotIn("runtime", sr._limits("guest"))
        with override_settings(SANDBOX_DOCKER_RUNTIME="runsc"):
            self.assertEqual(sr._limits(None)["runtime"], "runsc")
            self.assertEqual(sr._limits("guest")["runtime"], "runsc")

    def test_guest_timeouts_are_shorter(self):
        self.assertLess(sr._default_move_timeout("guest"), sr._default_move_timeout(None))
        self.assertLess(sr._default_verify_timeout("guest"), sr._default_verify_timeout(None))
        self.assertEqual((sr._default_move_timeout(None), sr._default_verify_timeout(None)), (30, 300))


class ContainerCreationTests(SimpleTestCase):
    def one_shot(self, profile):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(lambda kw: FakeContainer())
            with mock.patch.object(sr, "_docker_client", return_value=client):
                move = sr._run_predict_one_shot(model_dir_with_file(tmp), None, "fen", "w", "chess", profile=profile)
        return move, client.created[0]

    def test_one_shot_guest_gets_guest_limits_and_registered_keeps_old_ones(self):
        move, guest = self.one_shot("guest")
        self.assertEqual(move, "e2e4")
        self.assertEqual((guest["mem_limit"], guest["memswap_limit"], guest["pids_limit"]), ("256m", "256m", 64))
        self.assertIn("ulimits", guest)
        _, registered = self.one_shot(None)
        self.assertEqual((registered["mem_limit"], registered["pids_limit"]), ("512m", 128))
        self.assertNotIn("memswap_limit", registered)
        self.assertNotIn("ulimits", registered)
        for kw in (guest, registered):
            self.assertTrue(kw["network_disabled"] and kw["read_only"])
            self.assertEqual((kw["user"], kw["cap_drop"]), ("nobody", ["ALL"]))

    def test_check_uses_the_guest_limits_and_timeout(self):
        timeouts = {}
        for profile in ("guest", None):
            containers = []
            def make(kw, containers=containers):
                c = FakeContainer(logs=b'{"problems": [], "warnings": [], "moves": []}\n')
                containers.append(c)
                return c
            with tempfile.TemporaryDirectory() as tmp:
                client = FakeClient(make)
                with mock.patch.object(sr, "_docker_client", return_value=client):
                    result = sr.run_check_in_sandbox(model_dir_with_file(tmp), None, "chess", profile=profile)
            self.assertEqual(result["problems"], [])
            timeouts[profile] = containers[0].wait_timeout
            self.assertEqual("memswap_limit" in client.created[0], profile == "guest")
        self.assertEqual(timeouts, {"guest": 120, None: 300})


class WorkerStartTests(SimpleTestCase):
    def start(self, profile):
        with tempfile.TemporaryDirectory() as tmp:
            holder = {}

            def make(kw):
                ipc = next(h for h, spec in kw["volumes"].items() if spec["bind"] == "/ipc")
                holder["ipc"] = ipc
                return FakeContainer(on_start=lambda: Path(ipc, "ready.json").write_text(json.dumps({"init_s": 0.1})))

            client = FakeClient(make)
            with mock.patch.object(sr, "_docker_client", return_value=client):
                worker = sr._start_worker(("m", None, "chess"), (1, 1), model_dir_with_file(tmp), None, "chess", profile)
            try:
                return worker, client.created[0]
            finally:
                worker.stop()

    def test_guest_worker_is_labelled_limited_and_reads_capped(self):
        worker, kw = self.start("guest")
        self.assertEqual(worker.profile, "guest")
        self.assertEqual(worker.ipc_cap, 1024 * 1024)
        self.assertEqual(kw["labels"], {"agl.sandbox.worker": "1", "agl.sandbox.profile": "guest"})
        self.assertEqual(kw["memswap_limit"], kw["mem_limit"])

    def test_registered_worker_is_unchanged(self):
        worker, kw = self.start(None)
        self.assertIsNone(worker.ipc_cap)
        self.assertEqual(kw["labels"], {"agl.sandbox.worker": "1"})
        self.assertNotIn("ulimits", kw)


class IpcCapTests(SimpleTestCase):
    def test_read_json_never_returns_more_than_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            body = json.dumps({"move": "e2e4", "pad": "x" * 100})
            p.write_text(body)
            size = p.stat().st_size
            self.assertEqual(sr._read_json(p, size)["move"], "e2e4")
            self.assertIs(sr._read_json(p, size - 1), sr._TOO_LARGE)
            self.assertEqual(sr._read_json(p)["move"], "e2e4")
            p.write_text("{not json")
            self.assertIsNone(sr._read_json(p, 1000))
            self.assertIsNone(sr._read_json(Path(tmp) / "missing.json", 1000))

    def test_read_json_reads_at_most_cap_plus_one_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "huge.json"
            p.write_bytes(b" " * (5 * 1024 * 1024))
            real_open = open
            reads = []

            def spy(path, mode="r", *a, **kw):
                fh = real_open(path, mode, *a, **kw)
                orig = fh.read
                fh.read = lambda n=-1: (reads.append(n), orig(n))[1]
                return fh

            with mock.patch("builtins.open", spy):
                self.assertIs(sr._read_json(p, 1024), sr._TOO_LARGE)
        self.assertEqual(reads, [1025])

    def _worker(self, tmp, profile):
        root = Path(tmp)
        (root / "ipc").mkdir()
        return sr._Worker(("m", None, "chess"), (1, 1), FakeContainer(), root, profile)

    def _answer_with(self, worker, payload_factory):
        def run():
            req = worker.ipc_dir / "request.json"
            for _ in range(400):
                if req.exists():
                    seq = json.loads(req.read_text())["seq"]
                    (worker.ipc_dir / "response.json").write_text(payload_factory(seq))
                    return
                time.sleep(0.005)
        t = threading.Thread(target=run)
        t.start()
        return t

    def test_oversized_guest_response_is_refused_and_the_worker_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = self._worker(tmp, "guest")
            t = self._answer_with(worker, lambda seq: json.dumps({"seq": seq, "move": "e2e4", "pad": "x" * (2 * 1024 * 1024)}))
            resp, reason = sr._worker_request(worker, "fen", "w", 5)
            t.join()
        self.assertEqual((resp, reason), (None, "oversized"))

    def test_normal_guest_response_and_unchanged_registered_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            guest = self._worker(tmp, "guest")
            t = self._answer_with(guest, lambda seq: json.dumps({"seq": seq, "move": "e2e4"}))
            self.assertEqual(sr._worker_request(guest, "fen", "w", 5)[1], "ok")
            t.join()
        with tempfile.TemporaryDirectory() as tmp:
            registered = self._worker(tmp, None)
            big = lambda seq: json.dumps({"seq": seq, "move": "e2e4", "pad": "x" * (2 * 1024 * 1024)})
            t = self._answer_with(registered, big)
            resp, reason = sr._worker_request(registered, "fen", "w", 5)
            t.join()
        self.assertEqual((reason, resp["move"]), ("ok", "e2e4"))


@override_settings(SANDBOX_MAX_WORKERS=3, SANDBOX_GUEST_MAX_WORKERS=2)
class PoolRulesTests(SimpleTestCase):
    def setUp(self):
        sr._workers.clear()
        self.addCleanup(sr._workers.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = 0

    def add(self, profile, last_used):
        self.counter += 1
        base = (f"/m/{self.counter}", None, "chess")
        root = Path(self.tmp.name) / f"w{self.counter}"
        root.mkdir()
        worker = sr._Worker(base, (1, 1), mock.MagicMock(), root, profile)
        worker.last_used = last_used
        sr._workers[base] = worker
        return worker

    def ensure(self, profile):
        base = ("/m/new", None, "chess")
        created = sr._Worker(base, (1, 1), mock.MagicMock(), Path(self.tmp.name), profile)
        with mock.patch.object(sr, "_start_worker", return_value=created) as start:
            worker, cold = sr._ensure_worker_locked(base, (1, 1), Path("/m/new"), None, "chess", profile)
        return worker, start

    def test_guest_at_its_cap_evicts_only_its_own_oldest_worker(self):
        reg = self.add(None, 1)
        g_old, g_new = self.add("guest", 2), self.add("guest", 3)
        worker, start = self.ensure("guest")
        self.assertEqual(worker.profile, "guest")
        self.assertIn(reg.base, sr._workers)
        self.assertNotIn(g_old.base, sr._workers)
        self.assertIn(g_new.base, sr._workers)
        start.assert_called_once()

    def test_guest_never_evicts_a_registered_worker_even_when_the_pool_is_full(self):
        regs = [self.add(None, t) for t in (1, 2, 3)]
        with self.assertRaises(SandboxUnavailableError):
            self.ensure("guest")
        self.assertTrue(all(r.base in sr._workers for r in regs))

    def test_full_pool_with_one_guest_lets_a_guest_replace_only_that_guest(self):
        regs = [self.add(None, 1), self.add(None, 2)]
        guest = self.add("guest", 3)
        worker, _ = self.ensure("guest")
        self.assertNotIn(guest.base, sr._workers)
        self.assertTrue(all(r.base in sr._workers for r in regs))

    def test_registered_request_evicts_idle_guests_before_registered_workers(self):
        old_reg = self.add(None, 1)
        self.add(None, 2)
        guest = self.add("guest", 99)  # most recently used, still evicted first
        worker, _ = self.ensure(None)
        self.assertNotIn(guest.base, sr._workers)
        self.assertIn(old_reg.base, sr._workers)

    def test_registered_only_pool_still_evicts_the_least_recently_used(self):
        oldest = self.add(None, 1)
        self.add(None, 2)
        self.add(None, 3)
        worker, _ = self.ensure(None)
        self.assertNotIn(oldest.base, sr._workers)
        self.assertEqual(len(sr._workers), 3)
        self.assertIn(("/m/new", None, "chess"), sr._workers)


class GuestRealDirTests(TestCase):
    def test_guest_copies_live_in_a_per_repo_folder_and_are_removed_with_the_snapshot(self):
        from apps.users.guest_lifecycle import delete_guest_snapshots

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "um"
            hf = base / "hf_hub_cache"
            snap = hf / "models--alice--m" / "snapshots" / ("a" * 40)
            snap.mkdir(parents=True)
            (snap / "w.safetensors").write_text("weights")
            guest = User.objects.create_user(
                "guest_real0001", password=None, is_guest=True, hf_sub="s-real", guest_hf_username="alice",
                guest_expires_at=timezone.now() + timezone.timedelta(days=3),
            )
            UserGameModel.objects.create(user=guest, game_type="chess", hf_model_repo_id="alice/m", cached_path=str(snap))
            with override_settings(USER_MODELS_BASE_DIR=base, MODEL_CACHE_ROOT=base, HF_HUB_CACHE=str(hf)), \
                    mock.patch.object(Path, "is_symlink", lambda self: self.name == "w.safetensors"):
                guest_copy = sr._real_model_dir(snap, "guest")
                registered_copy = sr._real_model_dir(snap, None)
                self.assertEqual(guest_copy.parent, sr.guest_real_dir_root() / "models--alice--m")
                self.assertEqual(registered_copy.parent, base.parent / "sandbox_real")
                self.assertTrue((guest_copy / "w.safetensors").is_file())
                delete_guest_snapshots(guest.pk)
                self.assertFalse((sr.guest_real_dir_root() / "models--alice--m").exists())
                self.assertTrue(registered_copy.exists())


class ProfilePlumbingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.guest = User.objects.create_user(
            "guest_plumb001", password=None, is_guest=True, hf_sub="s-pl", guest_hf_username="alice", ai_name="alice",
            guest_expires_at=timezone.now() + timezone.timedelta(days=3),
        )
        self.reg = User.objects.create_user("plumbreg", password="pw")
        for user, repo in ((self.guest, "alice/m"), (self.reg, "plumbreg/m")):
            UserGameModel.objects.create(
                user=user, game_type="chess", hf_model_repo_id=repo, is_verified=True, status="active",
                approved_full_sha="a" * 40,
            )

    def test_inference_passes_the_guest_profile_only_for_guests(self):
        from apps.games import local_inference as li

        with tempfile.TemporaryDirectory() as tmp:
            md = model_dir_with_file(tmp)
            for user, expected in ((self.guest, {"profile": "guest"}), (self.reg, {})):
                gm = user.get_game_model("chess")
                with mock.patch.object(li, "_resolve_for_sandbox", return_value=(gm, md, None)), \
                        mock.patch("apps.games.sandbox_runner.run_predict_in_sandbox", return_value="e2e4") as run:
                    li.get_move_local(user.pk, "chess", "fen", "w")
                self.assertEqual(run.call_args.kwargs, expected)
                with mock.patch.object(li, "_resolve_for_sandbox", return_value=(gm, md, None)), \
                        mock.patch("apps.games.sandbox_runner.prewarm_worker", return_value=True) as warm:
                    li.prewarm_local(user.pk, "chess")
                self.assertEqual(warm.call_args.kwargs, expected)

    def test_model_check_passes_the_profile_only_when_given(self):
        from apps.games import model_check

        patches = dict(_load_manifest=lambda d: ({}, []), _check_manifest_fields=lambda *a: [], _check_syntax=lambda *a: [])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.multiple(model_check, **patches), \
                mock.patch("apps.games.sandbox_runner.run_check_in_sandbox", return_value={"problems": []}) as run:
            md = model_dir_with_file(tmp)
            model_check.check_model(md, None, "chess")
            self.assertEqual(run.call_args.kwargs, {})
            model_check.check_model(md, None, "chess", profile="guest")
            self.assertEqual(run.call_args.kwargs, {"profile": "guest"})

    def test_pinning_checks_a_guest_model_with_the_guest_profile(self):
        from apps.users.integrity import record_original_sha

        for user, expected in ((self.guest, {"profile": "guest"}), (self.reg, {})):
            gm = user.get_game_model("chess")
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch("apps.games.local_inference.resolve_model_path", return_value=(Path(tmp), None)), \
                    mock.patch("apps.games.model_check.check_model", return_value=[]) as check:
                record_original_sha(gm, False, sha="b" * 40)
            self.assertEqual(check.call_args.kwargs, expected)

    @override_settings(GUEST_MODE_ENABLED=True, GUEST_CELERY_QUEUE="guest")
    def test_guest_tasks_go_to_the_guest_queue(self):
        from apps.users import guest_snapshot as gs

        with mock.patch("apps.users.tasks.prepare_guest_snapshot_task.apply_async") as apply:
            gs.schedule_guest_prepare(5, "a" * 40, None, pin=False)
        self.assertEqual(apply.call_args.kwargs["queue"], "guest")
        self.assertEqual(apply.call_args.kwargs["args"], [5, "a" * 40, None, False])

    @override_settings(GUEST_MODE_ENABLED=True, GUEST_CELERY_QUEUE="guest")
    def test_diagnostics_for_guests_use_the_guest_queue_and_registered_use_delay(self):
        for user, uses_guest_queue in ((self.guest, True), (self.reg, False)):
            c = Client()
            c.force_login(user)
            with mock.patch("apps.users.tasks.run_diagnostics_task") as task:
                resp = c.post(reverse("users:run_diagnostics", args=["chess"]))
            self.assertEqual(resp.status_code, 200, resp.content)
            if uses_guest_queue:
                task.apply_async.assert_called_once()
                self.assertEqual(task.apply_async.call_args.kwargs["queue"], "guest")
                task.delay.assert_not_called()
            else:
                task.delay.assert_called_once()
                task.apply_async.assert_not_called()

    def test_default_guest_queue_is_the_normal_one(self):
        from django.conf import settings

        self.assertEqual(settings.GUEST_CELERY_QUEUE, "celery")


class GuestDiagnosticsTests(TestCase):
    def setUp(self):
        self.guest = User.objects.create_user(
            "guest_diag0001", password=None, is_guest=True, hf_sub="s-d", guest_hf_username="alice", ai_name="alice",
            guest_expires_at=timezone.now() + timezone.timedelta(days=3),
        )
        self.gm = UserGameModel.objects.create(
            user=self.guest, game_type="chess", hf_model_repo_id="alice/m", hf_data_repo_id="alice/d",
            is_verified=True, verification_method="oauth_namespace", status="active", approved_full_sha="a" * 40,
        )

    def run_it(self, check_result):
        from apps.users import guest_diagnostics as gd
        from apps.users.guest_snapshot import RepoListing

        run = DiagnosticRun.objects.create(user=self.guest, game_type="chess")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gd, "fetch_listing", return_value=RepoListing("a" * 40, (("a.py", 10),))), \
                mock.patch.object(gd.d, "_resolve_dirs_readonly", return_value=(Path(tmp), None)), \
                mock.patch("apps.games.model_check.check_model_verbose", return_value=check_result) as check:
            gd.run_guest_diagnostics(run.pk)
        run.refresh_from_db()
        return run, check

    def test_passing_run_uses_the_guest_profile_and_never_mentions_gating(self):
        run, check = self.run_it({"problems": [], "warnings": [], "moves": [{"position": 1, "move": "e2e4", "legal": True}], "stage": "sandbox"})
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.PASS)
        self.assertEqual(check.call_args.kwargs, {"profile": "guest"})
        text = " ".join(line["msg"] for line in run.lines).lower()
        for banned in ("gated", "agl_verify", "artificialgladiatorleague", "approve"):
            self.assertNotIn(banned, text)
        self.assertIn("public", text)

    def test_failing_check_is_reported(self):
        run, _ = self.run_it({"problems": ["'chess_mcvs.py' has a syntax error: x"], "warnings": [], "moves": [], "stage": "syntax"})
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        self.assertTrue(any("syntax error" in line["msg"] for line in run.lines))

    def test_task_dispatches_by_account_type(self):
        from apps.users.tasks import run_diagnostics_task

        run = DiagnosticRun.objects.create(user=self.guest, game_type="chess")
        with mock.patch("apps.users.guest_diagnostics.run_guest_diagnostics") as guest_runner, \
                mock.patch("apps.users.diagnostics.run_diagnostics") as normal_runner:
            run_diagnostics_task(run.pk)
        guest_runner.assert_called_once_with(run.pk)
        normal_runner.assert_not_called()
        User.objects.filter(pk=self.guest.pk).update(is_guest=False)
        with mock.patch("apps.users.guest_diagnostics.run_guest_diagnostics") as guest_runner, \
                mock.patch("apps.users.diagnostics.run_diagnostics") as normal_runner:
            run_diagnostics_task(run.pk)
        normal_runner.assert_called_once_with(run.pk)
        guest_runner.assert_not_called()
