"""Diagnostics tab: views, sanitizer, read-only run, platform-error handling, check_model_verbose.

All Hugging Face calls and Docker are mocked; nothing here needs network or Docker.
"""
from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.games import model_check
from apps.games.exceptions import SandboxUnavailableError
from apps.users import diagnostics
from apps.users.models import DiagnosticRun, UserGameModel

User = get_user_model()

PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)

GOOD_FILE_STATUS = {
    "model_files": [{"name": "chess_mcvs.py", "present": True}],
    "data_files": [{"name": "zone_db.npz", "present": True}],
    "model_error": "",
    "data_error": "",
    "revision_used": "a" * 40,
}

GOOD_MOVES = [
    {"position": 1, "fen": "f1", "player": "w", "move": "e2e4", "legal": True},
    {"position": 2, "fen": "f2", "player": "w", "move": "g1f3", "legal": True},
    {"position": 3, "fen": "f3", "player": "w", "move": "h1h8", "legal": True},
]

GOOD_VERBOSE = {"problems": [], "warnings": [], "moves": GOOD_MOVES, "stage": "sandbox"}


def _make_user(name: str):
    return User.objects.create_user(name, password="pw")


def _make_game_model(user, **over):
    fields = dict(
        user=user,
        game_type="chess",
        hf_model_repo_id=f"{user.username}/chess-model",
        hf_data_repo_id=f"{user.username}/chess-data",
        approved_full_sha="a" * 40,
        current_repo_sha="b" * 40,
        new_repo_sha="c" * 40,
        status=UserGameModel.ContractStatus.ACTIVE,
        last_error="",
        last_sandbox_error="old sandbox error",
        last_sandbox_error_at=timezone.now(),
        model_repo_ownership_verified=True,
        data_repo_ownership_verified=True,
        is_verified=True,
        verification_code="code-123",
        model_integrity_ok=True,
        repo_changed=False,
        rated_games_since_revalidation=12,
    )
    fields.update(over)
    return UserGameModel.objects.create(**fields)


def _make_run(user, game_type="chess", **over):
    return DiagnosticRun.objects.create(user=user, game_type=game_type, **over)


class DiagnosticsViewTests(TestCase):
    def setUp(self):
        self.user = _make_user("alice")
        self.other = _make_user("bob")
        _make_game_model(self.user)
        self.client.force_login(self.user)

    def _start_url(self, game="chess"):
        return reverse("users:run_diagnostics", args=[game])

    def test_start_requires_login(self):
        self.client.logout()
        resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/users/login/", resp["Location"])
        self.assertEqual(DiagnosticRun.objects.count(), 0)

    def test_status_and_latest_require_login(self):
        run = _make_run(self.user)
        self.client.logout()
        for url in (
            reverse("users:diagnostics_status", args=[run.pk]),
            reverse("users:diagnostics_latest", args=["chess"]),
        ):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("/users/login/", resp["Location"])

    def test_start_rejects_get(self):
        self.assertEqual(self.client.get(self._start_url()).status_code, 405)

    def test_start_rejects_unknown_game(self):
        self.assertEqual(self.client.post(self._start_url("checkers")).status_code, 400)

    def test_start_creates_run_and_enqueues_task(self):
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay:
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 200)
        run = DiagnosticRun.objects.get()
        self.assertEqual(resp.json(), {"run_id": run.pk})
        self.assertEqual((run.user, run.game_type, run.state), (self.user, "chess", "queued"))
        delay.assert_called_once_with(run.pk)

    def test_start_while_run_is_active_returns_429(self):
        _make_run(self.user, state=DiagnosticRun.State.RUNNING)
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay:
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 429)
        self.assertIn("already running", resp.json()["message"])
        delay.assert_not_called()
        self.assertEqual(DiagnosticRun.objects.count(), 1)

    def test_start_within_cooldown_returns_429(self):
        _make_run(self.user, state=DiagnosticRun.State.FINISHED)
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay:
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 429)
        self.assertIn("60 seconds", resp.json()["message"])
        delay.assert_not_called()

    def test_start_after_cooldown_is_allowed(self):
        run = _make_run(self.user, state=DiagnosticRun.State.FINISHED)
        DiagnosticRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(seconds=120))
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay"):
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 200)

    def test_cooldown_is_per_game(self):
        _make_run(self.user, game_type="breakthrough", state=DiagnosticRun.State.FINISHED)
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay"):
            resp = self.client.post(self._start_url("chess"))
        self.assertEqual(resp.status_code, 200)

    def test_stale_active_run_does_not_block_new_run(self):
        run = _make_run(self.user, state=DiagnosticRun.State.RUNNING)
        DiagnosticRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=1))
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay"):
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.state, DiagnosticRun.State.ERROR)

    def test_enqueue_failure_marks_run_error(self):
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay", side_effect=RuntimeError("broker down")):
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(DiagnosticRun.objects.get().state, DiagnosticRun.State.ERROR)

    def test_user_cannot_read_another_users_run(self):
        run = _make_run(self.other)
        resp = self.client.get(reverse("users:diagnostics_status", args=[run.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_status_respects_since(self):
        lines = [{"t": "10:00:0%d" % i, "level": "info", "msg": "line %d" % i} for i in range(5)]
        run = _make_run(self.user, state=DiagnosticRun.State.RUNNING, lines=lines)
        url = reverse("users:diagnostics_status", args=[run.pk])

        data = self.client.get(url).json()
        self.assertEqual(len(data["lines"]), 5)
        self.assertEqual(data["next"], 5)
        self.assertEqual(data["state"], "running")

        data = self.client.get(url, {"since": 3}).json()
        self.assertEqual([l["msg"] for l in data["lines"]], ["line 3", "line 4"])
        self.assertEqual(data["next"], 5)

        data = self.client.get(url, {"since": 99}).json()
        self.assertEqual(data["lines"], [])
        self.assertEqual(data["next"], 5)

        data = self.client.get(url, {"since": "garbage"}).json()
        self.assertEqual(len(data["lines"]), 5)

    def test_latest_returns_null_without_runs(self):
        resp = self.client.get(reverse("users:diagnostics_latest", args=["chess"]))
        self.assertEqual(resp.json(), {"run": None})

    def test_latest_returns_newest_run_for_this_user_and_game(self):
        _make_run(self.user, lines=[{"t": "1", "level": "info", "msg": "old"}], state="finished")
        newest = _make_run(self.user, lines=[{"t": "2", "level": "ok", "msg": "new"}], state="finished",
                           verdict="pass")
        _make_run(self.other, lines=[{"t": "3", "level": "info", "msg": "not mine"}])
        _make_run(self.user, game_type="breakthrough")
        data = self.client.get(reverse("users:diagnostics_latest", args=["chess"])).json()["run"]
        self.assertEqual(data["run_id"], newest.pk)
        self.assertEqual(data["verdict"], "pass")
        self.assertEqual([l["msg"] for l in data["lines"]], ["new"])

    def test_latest_rejects_unknown_game(self):
        self.assertEqual(self.client.get(reverse("users:diagnostics_latest", args=["checkers"])).status_code, 400)


@PLAIN_STATIC
class DiagnosticsTabRenderTests(TestCase):
    def setUp(self):
        self.user = _make_user("carol")
        self.viewer = _make_user("dave")

    def test_own_profile_shows_diagnostics_tab(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("users:profile")).content.decode()
        self.assertIn('data-tab="diagnostics"', html)
        self.assertIn('id="tab-diagnostics"', html)
        self.assertIn('data-diag-game="chess"', html)
        self.assertIn('data-diag-game="breakthrough"', html)
        self.assertIn(reverse("users:run_diagnostics", args=["chess"]), html)

    def test_public_profile_hides_diagnostics_tab(self):
        self.client.force_login(self.viewer)
        html = self.client.get(reverse("users:public_profile", args=[self.user.username])).content.decode()
        self.assertNotIn('data-tab="diagnostics"', html)
        self.assertNotIn("tab-diagnostics", html)
        self.assertNotIn("diag-run-btn", html)

    def test_profile_pages_do_not_show_my_gladiator_box(self):
        removed = ("Raised by", "belongs to you", "My Gladiator", "mood_display", "save_gladiator_field",
                   "saveField", "GLADIATOR_MOODS")
        self.client.force_login(self.user)
        for url in (reverse("users:profile"), reverse("users:public_profile", args=[self.viewer.username])):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            html = resp.content.decode()
            for text in removed:
                self.assertNotIn(text, html, f"{text!r} on {url}")

    @staticmethod
    def _panel(html, game):
        start = html.index(f'id="diag-game-{game}"')
        end = html.find('<div id="diag-game-', start + 1)
        return html[start:end] if end != -1 else html[start:]

    def test_panel_without_model_shows_message_and_disabled_button(self):
        _make_game_model(self.user)  # chess only; breakthrough has no model
        self.client.force_login(self.user)
        html = self.client.get(reverse("users:profile")).content.decode()
        message = diagnostics.get_missing_model_message(self.user, "breakthrough")

        bt = self._panel(html, "breakthrough")
        self.assertIn(message, bt)
        self.assertIn('data-no-model="1"', bt)
        self.assertIn("[WARN] " + message, bt)
        self.assertIn("Register a model in the Gladiator tab first.", bt)
        self.assertRegex(bt, r'<button[^>]*diag-run-btn[^>]*\sdisabled')

        chess = self._panel(html, "chess")
        self.assertNotIn("data-no-model", chess)
        self.assertNotIn("[WARN]", chess)
        self.assertRegex(chess, r'<button[^>]*diag-run-btn[^>]*>')
        self.assertNotRegex(chess, r'<button[^>]*diag-run-btn[^>]*\sdisabled')

    def test_panel_with_empty_repo_id_is_treated_as_no_model(self):
        _make_game_model(self.user, hf_model_repo_id="")
        self.client.force_login(self.user)
        chess = self._panel(self.client.get(reverse("users:profile")).content.decode(), "chess")
        self.assertIn('data-no-model="1"', chess)
        self.assertRegex(chess, r'<button[^>]*diag-run-btn[^>]*\sdisabled')


class MissingModelMessageTests(TestCase):
    def setUp(self):
        self.user = _make_user("frank")
        self.client.force_login(self.user)

    def _start_url(self, game="chess"):
        return reverse("users:run_diagnostics", args=[game])

    def _assert_no_model_response(self, resp):
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIs(data["no_model"], True)
        self.assertEqual(data["message"], diagnostics.get_missing_model_message(self.user, "chess"))
        self.assertIn("No Chess model is registered on your account.", data["message"])
        self.assertNotIn("run_id", data)

    def test_helper_returns_none_when_model_exists(self):
        _make_game_model(self.user)
        self.assertIsNone(diagnostics.get_missing_model_message(self.user, "chess"))

    def test_helper_message_uses_game_label(self):
        message = diagnostics.get_missing_model_message(self.user, "breakthrough")
        self.assertIn("No Breakthrough model is registered on your account.", message)
        self.assertIn("Connect Repo", message)

    def test_start_without_model_returns_message_and_creates_nothing(self):
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay, \
             mock.patch("apps.users.tasks.run_diagnostics_task.apply_async") as apply_async:
            resp = self.client.post(self._start_url())
        self._assert_no_model_response(resp)
        self.assertEqual(DiagnosticRun.objects.count(), 0)
        delay.assert_not_called()
        apply_async.assert_not_called()

    def test_start_with_empty_repo_id_behaves_the_same(self):
        _make_game_model(self.user, hf_model_repo_id="")
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay:
            resp = self.client.post(self._start_url())
        self._assert_no_model_response(resp)
        self.assertEqual(DiagnosticRun.objects.count(), 0)
        delay.assert_not_called()

    def test_no_model_check_happens_before_rate_limits(self):
        _make_run(self.user, state=DiagnosticRun.State.RUNNING)
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay:
            resp = self.client.post(self._start_url())
        self._assert_no_model_response(resp)
        delay.assert_not_called()

    def test_start_with_registered_model_still_queues_run(self):
        _make_game_model(self.user)
        with mock.patch("apps.users.tasks.run_diagnostics_task.delay") as delay:
            resp = self.client.post(self._start_url())
        self.assertEqual(resp.status_code, 200)
        run = DiagnosticRun.objects.get()
        self.assertEqual(resp.json(), {"run_id": run.pk})
        delay.assert_called_once_with(run.pk)

    def test_run_step_one_uses_the_shared_helper_message(self):
        run = _make_run(self.user)
        diagnostics.run_diagnostics(run.pk)
        run.refresh_from_db()
        message = diagnostics.get_missing_model_message(self.user, "chess")
        self.assertIn(message, [line["msg"] for line in run.lines])
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)


class SanitizeTests(TestCase):
    def test_removes_known_server_paths(self):
        from django.conf import settings

        text = f"cannot open {settings.BASE_DIR}/apps/x.py and {settings.USER_MODELS_BASE_DIR}/user_1/chess/model/a.py"
        out = diagnostics.sanitize(text)
        self.assertNotIn(str(settings.BASE_DIR), out)
        self.assertNotIn(str(settings.USER_MODELS_BASE_DIR), out)
        self.assertIn("<path>", out)

    def test_removes_generic_paths(self):
        out = diagnostics.sanitize(
            r"a C:\Users\bob\secret\file.py b /home/ubuntu/app/x.py c /tmp/agl_sandbox_abc/run.py d D:/data/x"
        )
        for leaked in ("bob", "ubuntu", "agl_sandbox_abc", "secret", "D:/"):
            self.assertNotIn(leaked, out)
        self.assertEqual(out.count("<path>"), 4)

    def test_keeps_sandbox_and_repo_paths(self):
        out = diagnostics.sanitize("data file not found in /data; repo alice/chess-model; /model/chess_mcvs.py")
        self.assertIn("/data", out)
        self.assertIn("alice/chess-model", out)
        self.assertIn("/model/chess_mcvs.py", out)

    def test_removes_hf_tokens(self):
        out = diagnostics.sanitize("token hf_abcdefghijklmnopqrstuvwxyz0123456789 and Bearer abcdefghijklmnop123")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", out)
        self.assertNotIn("abcdefghijklmnop123", out)

    @override_settings(HF_PLATFORM_TOKEN="platform-secret-value")
    def test_removes_platform_token(self):
        self.assertNotIn("platform-secret-value", diagnostics.sanitize("oops platform-secret-value leaked"))

    def test_collapses_whitespace_and_truncates(self):
        self.assertEqual(diagnostics.sanitize("a \n\t  b\n\nc"), "a b c")
        out = diagnostics.sanitize("x" * 5000)
        self.assertLessEqual(len(out), diagnostics.MAX_MESSAGE_LEN)
        self.assertTrue(out.endswith("..."))

    def test_handles_none(self):
        self.assertEqual(diagnostics.sanitize(None), "")


class RunDiagnosticsTests(TestCase):
    def setUp(self):
        self.user = _make_user("erin")
        self.gm = _make_game_model(self.user)
        self._tmp = tempfile.TemporaryDirectory(prefix="agl_test_diag_")
        self.addCleanup(self._tmp.cleanup)
        self.model_dir = Path(self._tmp.name) / "model"
        self.model_dir.mkdir()

    def _run(self, *, gated=None, file_status=GOOD_FILE_STATUS, resolved=True, verbose=GOOD_VERBOSE,
             verbose_side_effect=None):
        run = _make_run(self.user)
        resolve_value = (self.model_dir, None) if resolved else (None, None)
        with mock.patch("apps.users.views._check_repo_is_gated", return_value=gated), \
             mock.patch("apps.users.views._build_breakthrough_file_status", return_value=file_status), \
             mock.patch("apps.games.local_inference.resolve_model_path", return_value=resolve_value), \
             mock.patch("apps.games.model_check.check_model_verbose", return_value=verbose,
                        side_effect=verbose_side_effect):
            diagnostics.run_diagnostics(run.pk)
        run.refresh_from_db()
        return run

    @staticmethod
    def _msgs(run):
        return [line["msg"] for line in run.lines]

    def test_happy_path_reports_model_works(self):
        run = self._run()
        self.assertEqual(run.state, DiagnosticRun.State.FINISHED)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.PASS)
        self.assertEqual(run.lines[-1]["msg"], "RESULT: your model works correctly")
        self.assertEqual(run.lines[-1]["level"], "ok")
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        for line in run.lines:
            self.assertEqual(set(line), {"t", "level", "msg"})
            self.assertRegex(line["t"], r"^\d\d:\d\d:\d\d$")
            self.assertIn(line["level"], {"info", "ok", "warn", "fail"})
        joined = "\n".join(self._msgs(run))
        self.assertIn("your model played e2e4 (legal)", joined)
        self.assertIn("Present: chess_mcvs.py", joined)
        self.assertIn("aaaaaaaaaaaa", joined)
        self.assertNotIn("a" * 13, joined)  # SHAs are truncated to 12 chars

    def test_status_saved_during_the_run_is_seen_by_the_later_steps(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(status="pending")

        def slow_file_status(*args, **kwargs):
            # e.g. the user clicks "Verify Ownership" in another tab while step 4 is fetching from Hugging Face
            UserGameModel.objects.filter(pk=self.gm.pk).update(status="active")
            return GOOD_FILE_STATUS

        run = _make_run(self.user)
        with mock.patch("apps.users.views._check_repo_is_gated", return_value=None), \
             mock.patch("apps.users.views._build_breakthrough_file_status", side_effect=slow_file_status), \
             mock.patch("apps.games.local_inference.resolve_model_path", return_value=(self.model_dir, None)), \
             mock.patch("apps.games.model_check.check_model_verbose", return_value=GOOD_VERBOSE):
            diagnostics.run_diagnostics(run.pk)
        run.refresh_from_db()
        self.assertIn("Stored status: active", self._msgs(run))
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.PASS)

    def test_pending_status_does_not_tell_the_user_to_wait(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(status="pending")
        run = self._run()
        msgs = "\n".join(self._msgs(run))
        self.assertIn("click 'Verify Ownership'", msgs)
        self.assertNotIn("Wait for the platform check", msgs)

    def test_run_never_modifies_user_game_model(self):
        before = UserGameModel.objects.filter(pk=self.gm.pk).values().get()
        with mock.patch.object(UserGameModel, "save", side_effect=AssertionError("save() must not be called")):
            run = self._run()
        self.assertEqual(run.state, DiagnosticRun.State.FINISHED)
        after = UserGameModel.objects.filter(pk=self.gm.pk).values().get()
        self.assertEqual(before, after)

    def test_failing_run_does_not_modify_user_game_model_either(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(
            status="failed", last_error="bad", model_repo_ownership_verified=False, model_integrity_ok=False,
        )
        before = UserGameModel.objects.filter(pk=self.gm.pk).values().get()
        bad = {"problems": ["sample position 1: 'e2e5' is not a legal move for this position"],
               "warnings": [], "stage": "sandbox",
               "moves": [{"position": 1, "fen": "f", "player": "w", "move": "e2e5", "legal": False}]}
        with mock.patch.object(UserGameModel, "save", side_effect=AssertionError("save() must not be called")):
            run = self._run(verbose=bad)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        self.assertEqual(before, UserGameModel.objects.filter(pk=self.gm.pk).values().get())

    def test_no_registered_model_stops_with_clear_message(self):
        UserGameModel.objects.all().delete()
        run = self._run()
        self.assertEqual(run.state, DiagnosticRun.State.FINISHED)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        msgs = self._msgs(run)
        self.assertTrue(any("No Chess model is registered" in m for m in msgs))
        self.assertIn("RESULT: problems found", msgs)
        self.assertFalse(any(m.startswith("Step 2/") for m in msgs))

    def test_repo_access_error_is_reported_with_profile_wording(self):
        message = ("Required: Grant Access to Our Platform Account — your repo must be Gated and you must "
                   "approve 'ArtificialGladiatorLeague' in the Access Settings of your Hugging Face repo, "
                   "otherwise your submission will be rejected.")
        run = self._run(gated=message)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        fails = [l["msg"] for l in run.lines if l["level"] == "fail"]
        self.assertTrue(any(m.startswith("Required: Grant Access to Our Platform Account") for m in fails))

    def test_hf_unreachable_is_platform_error_not_fail(self):
        run = self._run(gated="Could not reach Hugging Face to verify the repository. Please try again in a moment.")
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.PLATFORM_ERROR)

    def test_missing_ownership_is_reported_not_rechecked(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(
            model_repo_ownership_verified=False, data_repo_ownership_verified=False, is_verified=False,
        )
        with mock.patch("apps.users.ownership_verification.check_full_ownership") as recheck:
            run = self._run()
        recheck.assert_not_called()
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        msgs = self._msgs(run)
        self.assertIn("Ownership of the model repo: missing", msgs)
        self.assertIn("Ownership of the data repo: missing", msgs)
        self.assertTrue(any("AGL_VERIFY.txt" in m and "Verify Ownership" in m for m in msgs))

    def test_missing_repo_files_are_listed(self):
        status = dict(GOOD_FILE_STATUS, model_files=[],
                      model_error="The model repo is missing files listed in config_model.json: a.py, w.npz.")
        run = self._run(file_status=status)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        msgs = self._msgs(run)
        self.assertIn("Missing: a.py", msgs)
        self.assertIn("Missing: w.npz", msgs)

    def test_unreadable_config_reports_error_text(self):
        status = dict(GOOD_FILE_STATUS, data_files=[], data_error="Data repo 'erin/chess-data' not found.")
        run = self._run(file_status=status)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        self.assertIn("Data repo 'erin/chess-data' not found.", self._msgs(run))

    def test_cooldown_and_integrity_are_flagged(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(
            repo_changed=True, rated_games_since_revalidation=4, model_integrity_ok=False,
        )
        run = self._run()
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        warns = [l["msg"] for l in run.lines if l["level"] == "warn"]
        self.assertTrue(any("cooldown is active" in m for m in warns))
        self.assertTrue(any("integrity: NOT OK" in m for m in warns))
        self.assertIn("Rated games since last revalidation: 4 / 30", self._msgs(run))

    def test_non_active_status_prevents_ok_verdict(self):
        UserGameModel.objects.filter(pk=self.gm.pk).update(status="failed", last_error="boom")
        run = self._run()
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        self.assertTrue(any("Last stored error: boom" in m for m in self._msgs(run)))

    def test_missing_local_snapshot_skips_live_test(self):
        with mock.patch("apps.games.model_check.check_model_verbose") as live:
            run = self._run(resolved=False)
        live.assert_not_called()
        self.assertNotEqual(run.verdict, DiagnosticRun.Verdict.PASS)
        warns = [l["msg"] for l in run.lines if l["level"] == "warn"]
        self.assertTrue(any("approved revision" in m and "are not on the platform" in m for m in warns))
        msgs = self._msgs(run)
        self.assertTrue(any("log out and log in again" in m for m in msgs))
        self.assertFalse(any("Wait for the platform" in m for m in msgs))

    def test_illegal_move_is_reported_per_position(self):
        bad = {"problems": ["sample position 2: 'e2e5' is not a legal move for this position"],
               "warnings": ["sample position 3 took 20.0s (slow)"], "stage": "sandbox",
               "moves": [GOOD_MOVES[0],
                         {"position": 2, "fen": "f", "player": "w", "move": "e2e5", "legal": False},
                         GOOD_MOVES[2]]}
        run = self._run(verbose=bad)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        msgs = self._msgs(run)
        self.assertIn("Sample position 2: your model played e2e5 (ILLEGAL move)", msgs)
        self.assertIn("sample position 3 took 20.0s (slow)", msgs)
        self.assertIn("RESULT: problems found", msgs)
        self.assertTrue(any(m.startswith("1. ") for m in msgs))
        self.assertTrue(any(m.startswith("Fix: ") for m in msgs))

    def test_syntax_stage_problem_is_reported(self):
        bad = {"problems": ["'chess_mcvs.py' has a syntax error: invalid syntax"], "warnings": [],
               "moves": [], "stage": "syntax"}
        run = self._run(verbose=bad)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.FAIL)
        msgs = self._msgs(run)
        self.assertIn("Manifest check passed.", msgs)
        self.assertTrue(any("syntax error" in m for m in msgs))

    def test_sandbox_unavailable_gives_platform_error(self):
        run = self._run(verbose_side_effect=SandboxUnavailableError("docker down at /var/run/docker.sock"))
        self.assertEqual(run.state, DiagnosticRun.State.FINISHED)
        self.assertEqual(run.verdict, DiagnosticRun.Verdict.PLATFORM_ERROR)
        warns = [l["msg"] for l in run.lines if l["level"] == "warn"]
        self.assertTrue(any("temporarily unavailable" in m and "not a problem with your model" in m for m in warns))
        self.assertNotIn("RESULT: problems found", self._msgs(run))
        self.assertNotIn("docker.sock", "\n".join(self._msgs(run)))

    def test_unexpected_exception_sets_error_state_and_hides_traceback(self):
        run = _make_run(self.user)
        with mock.patch("apps.users.diagnostics._step_ownership", side_effect=RuntimeError("secret /home/x/y")), \
             mock.patch("apps.users.views._check_repo_is_gated", return_value=None), \
             self.assertLogs("apps.users.diagnostics", level="ERROR") as logs:
            diagnostics.run_diagnostics(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, DiagnosticRun.State.ERROR)
        self.assertEqual(run.verdict, "")
        self.assertEqual(run.lines[-1]["level"], "fail")
        self.assertEqual(run.lines[-1]["msg"], "Diagnostics failed unexpectedly, please try again later.")
        joined = "\n".join(self._msgs(run))
        self.assertNotIn("Traceback", joined)
        self.assertNotIn("secret", joined)
        self.assertTrue(any("RuntimeError" in line for line in logs.output))

    def test_lines_are_saved_progressively(self):
        run = _make_run(self.user)
        snapshots = []

        def spy(*args, **kwargs):
            snapshots.append(DiagnosticRun.objects.get(pk=run.pk).lines[:])
            return None

        with mock.patch("apps.users.views._check_repo_is_gated", side_effect=spy), \
             mock.patch("apps.users.views._build_breakthrough_file_status", return_value=GOOD_FILE_STATUS), \
             mock.patch("apps.games.local_inference.resolve_model_path", return_value=(self.model_dir, None)), \
             mock.patch("apps.games.model_check.check_model_verbose", return_value=GOOD_VERBOSE):
            diagnostics.run_diagnostics(run.pk)
        self.assertEqual(len(snapshots), 1)
        self.assertGreaterEqual(len(snapshots[0]), 3)  # step 1 lines were visible before step 2 ran
        self.assertLess(len(snapshots[0]), len(DiagnosticRun.objects.get(pk=run.pk).lines))

    def test_only_five_newest_runs_are_kept_per_user_and_game(self):
        for _ in range(6):
            _make_run(self.user, state=DiagnosticRun.State.FINISHED)
        _make_run(self.user, game_type="breakthrough", state=DiagnosticRun.State.FINISHED)
        run = self._run()
        chess = DiagnosticRun.objects.filter(user=self.user, game_type="chess")
        self.assertEqual(chess.count(), 5)
        self.assertTrue(chess.filter(pk=run.pk).exists())
        self.assertEqual(DiagnosticRun.objects.filter(user=self.user, game_type="breakthrough").count(), 1)

    def test_finished_run_is_not_executed_twice(self):
        run = _make_run(self.user, state=DiagnosticRun.State.FINISHED)
        with mock.patch("apps.users.diagnostics._execute") as execute:
            diagnostics.run_diagnostics(run.pk)
        execute.assert_not_called()

    def test_no_secret_or_server_path_in_any_line(self):
        from django.conf import settings

        UserGameModel.objects.filter(pk=self.gm.pk).update(
            last_sandbox_error=f"failed at {settings.BASE_DIR}/x.py with hf_abcdefghijklmnopqrstuvwxyz",
        )
        with override_settings(HF_PLATFORM_TOKEN="platform-secret-value"):
            run = self._run()
        joined = "\n".join(self._msgs(run))
        self.assertNotIn(str(settings.BASE_DIR), joined)
        self.assertNotIn("hf_abcdefghijklmnopqrstuvwxyz", joined)
        self.assertNotIn("platform-secret-value", joined)


class DiagnosticsTaskTests(TestCase):
    def setUp(self):
        self.user = _make_user("frank")

    def test_task_runs_diagnostics(self):
        from apps.users.tasks import run_diagnostics_task

        with mock.patch("apps.users.diagnostics.run_diagnostics") as run:
            run_diagnostics_task(123)
        run.assert_called_once_with(123)

    def test_task_has_time_limits(self):
        from apps.users.tasks import run_diagnostics_task

        self.assertEqual(run_diagnostics_task.soft_time_limit, 400)

    def test_soft_timeout_marks_run_as_error(self):
        from apps.users.tasks import run_diagnostics_task

        run = _make_run(self.user, state=DiagnosticRun.State.RUNNING)
        with mock.patch("apps.users.diagnostics.run_diagnostics", side_effect=SoftTimeLimitExceeded()):
            run_diagnostics_task(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, DiagnosticRun.State.ERROR)
        self.assertIn("timed out", run.lines[-1]["msg"])
        self.assertEqual(run.lines[-1]["level"], "fail")

    def test_task_is_registered_with_celery(self):
        from agladiator.celery import app

        app.loader.import_default_modules()
        self.assertIn("apps.users.tasks.run_diagnostics_task", app.tasks)


class CheckModelVerboseTests(TestCase):
    """check_model() must return exactly check_model_verbose()["problems"] for every case."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agl_test_verbose_")
        self.addCleanup(self._tmp.cleanup)
        self.model_dir = Path(self._tmp.name) / "model"
        self.model_dir.mkdir()

    def _write(self, name, content):
        (self.model_dir / name).write_text(content, encoding="utf-8")

    def _both(self, model_dir=None, data_dir=None, sandbox=None):
        model_dir = model_dir or self.model_dir
        with mock.patch("apps.games.sandbox_runner.run_check_in_sandbox",
                        return_value=sandbox or {"problems": [], "warnings": []}):
            problems = model_check.check_model(model_dir, data_dir, "chess")
            verbose = model_check.check_model_verbose(model_dir, data_dir, "chess")
        self.assertEqual(problems, verbose["problems"])
        self.assertEqual(set(verbose), {"problems", "warnings", "moves", "stage"})
        return verbose

    def test_missing_model_dir(self):
        verbose = self._both(model_dir=self.model_dir / "nope")
        self.assertEqual(verbose["stage"], "manifest")
        self.assertTrue(any("does not exist" in p for p in verbose["problems"]))

    def test_invalid_json_manifest(self):
        self._write("config_model.json", "{not valid json")
        self._write("chess_mcvs.py", "def load(ctx): return {}\n")
        verbose = self._both()
        self.assertEqual(verbose["stage"], "manifest")
        self.assertTrue(any("not valid JSON" in p for p in verbose["problems"]))

    def test_manifest_field_errors(self):
        self._write("config_model.json", json.dumps({"schema_version": "one", "data_files": "x"}))
        self._write("chess_mcvs.py", "def load(ctx): return {}\n")
        self.assertEqual(self._both()["stage"], "manifest")

    def test_missing_data_file(self):
        self._write("config_model.json", json.dumps({"data_files": ["zone_db.npz"]}))
        self._write("chess_mcvs.py", "def load(ctx): return {}\n")
        verbose = self._both()
        self.assertEqual(verbose["stage"], "manifest")
        self.assertTrue(any("zone_db.npz" in p for p in verbose["problems"]))

    def test_syntax_error(self):
        self._write("chess_mcvs.py", "def load(ctx)\n    return {}\n")
        verbose = self._both()
        self.assertEqual(verbose["stage"], "syntax")
        self.assertTrue(any("syntax error" in p for p in verbose["problems"]))

    def test_no_python_file(self):
        verbose = self._both()
        self.assertEqual(verbose["stage"], "syntax")
        self.assertTrue(any("no .py model file found" in p for p in verbose["problems"]))

    def test_clean_model_passes_moves_and_warnings_through(self):
        self._write("chess_mcvs.py", "def load(ctx): return {}\ndef get_move(s, f, p): return 'e2e4'\n")
        moves = [{"position": 1, "fen": "f", "player": "w", "move": "e2e4", "legal": True}]
        verbose = self._both(sandbox={"problems": [], "warnings": ["slow"], "moves": moves})
        self.assertEqual(verbose["stage"], "sandbox")
        self.assertEqual(verbose["problems"], [])
        self.assertEqual(verbose["warnings"], ["slow"])
        self.assertEqual(verbose["moves"], moves)

    def test_sandbox_problems_are_returned(self):
        self._write("chess_mcvs.py", "def load(ctx): return {}\ndef get_move(s, f, p): return 'e2e4'\n")
        verbose = self._both(sandbox={"problems": ["sample position 1: model returned no move"], "warnings": []})
        self.assertEqual(verbose["problems"], ["sample position 1: model returned no move"])
        self.assertEqual(verbose["moves"], [])

    def test_sandbox_unavailable_propagates_from_both(self):
        self._write("chess_mcvs.py", "def load(ctx): return {}\ndef get_move(s, f, p): return 'e2e4'\n")
        with mock.patch("apps.games.sandbox_runner.run_check_in_sandbox",
                        side_effect=SandboxUnavailableError("docker down")):
            with self.assertRaises(SandboxUnavailableError):
                model_check.check_model(self.model_dir, None, "chess")
            with self.assertRaises(SandboxUnavailableError):
                model_check.check_model_verbose(self.model_dir, None, "chess")
