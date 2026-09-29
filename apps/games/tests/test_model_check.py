"""Tests for apps.games.model_check.check_model().

Covers the host-side (no Docker) checks — syntax, manifest fields, and data
file existence — plus the delegation into
apps.games.sandbox_runner.run_check_in_sandbox() for the execution-based
checks (mocked here so these tests never need Docker).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from apps.games import model_check
from apps.games.exceptions import SandboxUnavailableError


class CheckModelHostSideTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agl_test_model_check_")
        self.addCleanup(self._tmp.cleanup)
        self.model_dir = Path(self._tmp.name) / "model"
        self.model_dir.mkdir()

    def _write(self, name: str, content: str) -> None:
        (self.model_dir / name).write_text(content, encoding="utf-8")

    def test_missing_model_dir(self):
        problems = model_check.check_model(self.model_dir / "nope", None, "chess")
        self.assertTrue(any("does not exist" in p for p in problems))

    def test_invalid_json_manifest(self):
        self._write("config_model.json", "{not valid json")
        self._write("chess_mcvs.py", "def load(ctx): return {}\n")
        problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertTrue(any("not valid JSON" in p for p in problems))

    def test_manifest_field_type_errors(self):
        self._write("config_model.json", json.dumps({
            "schema_version": "one",
            "data_files": "not-a-list",
        }))
        self._write("chess_mcvs.py", "def load(ctx): return {}\n")
        problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertTrue(any("schema_version" in p for p in problems))
        self.assertTrue(any("data_files" in p for p in problems))

    def test_missing_entrypoint_reported(self):
        self._write("config_model.json", json.dumps({"entrypoint": "missing.py"}))
        problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertTrue(any("missing.py" in p and "does not exist" in p for p in problems))

    def test_missing_data_file_reported(self):
        self._write("config_model.json", json.dumps({"data_files": ["zone_db.npz"]}))
        self._write("chess_mcvs.py", "def load(ctx): return {}\n")
        problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertTrue(any("zone_db.npz" in p for p in problems))

    def test_syntax_error_reported(self):
        self._write("chess_mcvs.py", "def load(ctx)\n    return {}\n")  # missing colon
        problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertTrue(any("syntax error" in p for p in problems))

    def test_no_python_file_found(self):
        problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertTrue(any("no .py model file found" in p for p in problems))

    def test_clean_model_delegates_to_sandbox_and_passes(self):
        self._write("chess_mcvs.py", "def load(ctx): return {}\ndef get_move(s, f, p): return 'e2e4'\n")
        with mock.patch(
            "apps.games.sandbox_runner.run_check_in_sandbox",
            return_value={"problems": [], "warnings": []},
        ) as mocked:
            problems = model_check.check_model(self.model_dir, None, "chess")
        mocked.assert_called_once()
        self.assertEqual(problems, [])

    def test_sandbox_problems_propagate(self):
        self._write("chess_mcvs.py", "def load(ctx): return {}\ndef get_move(s, f, p): return 'e2e4'\n")
        with mock.patch(
            "apps.games.sandbox_runner.run_check_in_sandbox",
            return_value={"problems": ["sample position 1: illegal move"], "warnings": []},
        ):
            problems = model_check.check_model(self.model_dir, None, "chess")
        self.assertEqual(problems, ["sample position 1: illegal move"])

    def test_sandbox_unavailable_propagates_as_exception(self):
        self._write("chess_mcvs.py", "def load(ctx): return {}\ndef get_move(s, f, p): return 'e2e4'\n")
        with mock.patch(
            "apps.games.sandbox_runner.run_check_in_sandbox",
            side_effect=SandboxUnavailableError("docker down"),
        ):
            with self.assertRaises(SandboxUnavailableError):
                model_check.check_model(self.model_dir, None, "chess")

    def test_static_problems_skip_sandbox_call(self):
        self._write("chess_mcvs.py", "def load(ctx:\n")  # syntax error
        with mock.patch("apps.games.sandbox_runner.run_check_in_sandbox") as mocked:
            problems = model_check.check_model(self.model_dir, None, "chess")
        mocked.assert_not_called()
        self.assertTrue(problems)
