"""Tests for the in-container scripts embedded in apps.games.sandbox_runner.

_RUNNER_SCRIPT and _CHECK_RUNNER_SCRIPT are plain Python source strings that
get written to a file and executed inside the Docker sandbox in production.
These tests run them directly with `subprocess` against a real temp
directory (no Docker needed) by pointing AGL_MODEL_DIR/AGL_DATA_DIR at test
fixtures, to validate the load(ctx)/get_move(state, fen, player) dispatch
and sample-position legality logic without needing a container.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from apps.games import sandbox_runner


def _write_model(model_dir: Path, filename: str, code: str) -> None:
    (model_dir / filename).write_text(code, encoding="utf-8")


def _run_script(script: str, model_dir: Path, game_type: str, extra_env: dict | None = None) -> dict:
    script_path = model_dir.parent / "run_script.py"
    script_path.write_text(script, encoding="utf-8")
    import os
    env = dict(os.environ)
    env["AGL_MODEL_DIR"] = str(model_dir)
    env["AGL_GAME_TYPE"] = game_type
    env.pop("AGL_DATA_DIR", None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(script_path)], capture_output=True, text=True, env=env,
    )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    assert lines, f"no output; stderr={result.stderr}"
    return json.loads(lines[-1])


class MoveRunnerScriptTests(SimpleTestCase):
    databases = []

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agl_test_runner_")
        self.addCleanup(self._tmp.cleanup)
        self.model_dir = Path(self._tmp.name) / "model"
        self.model_dir.mkdir()

    def test_new_style_load_get_move(self):
        _write_model(self.model_dir, "chess_mcvs.py", """
def load(ctx):
    return {"ready": True}

def get_move(state, fen, player):
    assert state["ready"]
    return "e2e4"
""")
        out = _run_script(
            sandbox_runner._RUNNER_SCRIPT, self.model_dir, "chess",
            extra_env={"AGL_FEN": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "AGL_PLAYER": "w"},
        )
        self.assertEqual(out.get("move"), "e2e4")

    def test_no_load_entrypoint_reports_error(self):
        _write_model(self.model_dir, "model.py", """
def get_move(fen, player, zone_db=None):
    return "e2e4"
""")
        out = _run_script(
            sandbox_runner._RUNNER_SCRIPT, self.model_dir, "chess",
            extra_env={"AGL_FEN": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "AGL_PLAYER": "w"},
        )
        self.assertIn("error", out)

    def test_no_move_reports_error(self):
        _write_model(self.model_dir, "model.py", """
def load(ctx):
    return {}

def get_move(state, fen, player):
    return None
""")
        out = _run_script(
            sandbox_runner._RUNNER_SCRIPT, self.model_dir, "chess",
            extra_env={"AGL_FEN": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "AGL_PLAYER": "w"},
        )
        self.assertIn("error", out)


class CheckRunnerScriptTests(SimpleTestCase):
    databases = []

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agl_test_check_runner_")
        self.addCleanup(self._tmp.cleanup)
        self.model_dir = Path(self._tmp.name) / "model"
        self.model_dir.mkdir()

    def test_good_model_has_no_problems(self):
        _write_model(self.model_dir, "chess_mcvs.py", """
import chess

def load(ctx):
    return {}

def get_move(state, fen, player):
    b = chess.Board(fen)
    return str(next(iter(b.legal_moves)))
""")
        out = _run_script(sandbox_runner._CHECK_RUNNER_SCRIPT, self.model_dir, "chess")
        self.assertEqual(out.get("problems"), [])

    def test_illegal_move_is_flagged(self):
        _write_model(self.model_dir, "model.py", """
def load(ctx):
    return {}

def get_move(state, fen, player):
    return "a1a1"
""")
        out = _run_script(sandbox_runner._CHECK_RUNNER_SCRIPT, self.model_dir, "chess")
        self.assertTrue(out["problems"])
        self.assertTrue(all("not a legal move" in p for p in out["problems"]))

    def test_exception_during_get_move_is_flagged(self):
        _write_model(self.model_dir, "model.py", """
def load(ctx):
    return {}

def get_move(state, fen, player):
    raise RuntimeError("boom")
""")
        out = _run_script(sandbox_runner._CHECK_RUNNER_SCRIPT, self.model_dir, "chess")
        self.assertTrue(any("boom" in p for p in out["problems"]))

    def test_no_load_entrypoint_is_flagged(self):
        _write_model(self.model_dir, "model.py", """
def get_move(fen, player, zone_db=None):
    return "a1a1"
""")
        out = _run_script(sandbox_runner._CHECK_RUNNER_SCRIPT, self.model_dir, "chess")
        self.assertTrue(any("no load(ctx) entrypoint" in p for p in out["problems"]))

    def test_breakthrough_legality_checked(self):
        _write_model(self.model_dir, "breakthrough_mcvs.py", """
def load(ctx):
    return {}

def get_move(state, fen, player):
    return "a2a3" if player == "w" else "a7a6"
""")
        out = _run_script(sandbox_runner._CHECK_RUNNER_SCRIPT, self.model_dir, "breakthrough")
        # The 3rd sample position has no piece on a2, so the hardcoded
        # move must be flagged as illegal there.
        self.assertTrue(any("sample position 3" in p for p in out["problems"]))

    def test_missing_data_file_is_flagged(self):
        (self.model_dir / "config_model.json").write_text(
            json.dumps({"modules": ["model.py"], "data_files": ["zone_db.npz"]}), encoding="utf-8",
        )
        _write_model(self.model_dir, "model.py", """
import chess

def load(ctx):
    return {}

def get_move(state, fen, player):
    b = chess.Board(fen)
    return str(next(iter(b.legal_moves)))
""")
        out = _run_script(sandbox_runner._CHECK_RUNNER_SCRIPT, self.model_dir, "chess")
        self.assertTrue(any("zone_db.npz" in p for p in out["problems"]))
