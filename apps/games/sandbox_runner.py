# ──────────────────────────────────────────────
# apps/games/sandbox_runner.py
#
# Docker sandbox — runs a user's model inside an isolated,
# network-disabled container to get a single move.
#
# Docker is MANDATORY. There is no in-process / local-process
# fallback: if the Docker daemon or sandbox image is unavailable,
# SandboxUnavailableError is raised and MUST propagate (no random
# move, no execution of user code outside the container).
#
# Container hardening
# ────────────────────
#   • network_disabled=True        — no network access at all
#   • read_only=True + tmpfs /tmp  — root filesystem is immutable
#   • model/data/runner mounted read-only
#   • cap_drop=["ALL"], no-new-privileges, non-root user
#   • mem_limit / nano_cpus / pids_limit — resource exhaustion guard
#   • settings.SANDBOX_MOVE_TIMEOUT — hard wall-clock timeout
#
# Encoding tolerance
# ──────────────────
#   User files saved on Windows are often cp1255 / cp1252, not UTF-8.
#   The runner decodes every model .py itself (utf-8-sig -> cp1255 ->
#   cp1252 -> latin-1) and compiles the text in memory. The mounted model
#   files are NEVER modified (SHA pinning stays valid). Any fallback is
#   reported back in the JSON "warnings" field and logged by the host.
#
# Manifest (matches apps/games/handler.py + apps/games/model_check.py)
# ────────────────────────────────────────────────────────────────────
#   Optional /model/config_model.json is a small, VERSIONED, fixed contract.
#   The platform never interprets user data beyond these fields — everything
#   else about the model's internals is opaque to the platform. All fields
#   are optional; a missing field falls back to today's behavior so existing
#   models keep working unmodified:
#
#     schema_version    int,    default 1
#     game_type         str,    default — whatever the caller passed in
#     entrypoint        str,    default "<game_type>_mcvs.py"
#     modules           list[str], legacy alternative to "entrypoint" —
#                       every listed file is imported, the LAST one wins.
#                       When neither "entrypoint" nor "modules" is present,
#                       every *.py in /model is imported (legacy default).
#     data_files        list[str], data files the model expects in /data
#                       (checked by apps.games.model_check, not the runner).
#     requirements      list[str], informational only (pip packages the
#                       model needs — the sandbox image is fixed, this is
#                       just surfaced to the user during the model check).
#
#   Single entrypoint contract — the module MUST define ``load``:
#        state = load(ctx)                  # called ONCE; ctx.model_dir and
#                                            # ctx.data_dir are real,
#                                            # read-only folders (Path).
#        move  = get_move(state, fen, player)
#      The platform does NOT open zone_db or any other data file itself —
#      load() is responsible for reading its own files from ctx.data_dir.
#      There is no fallback interface; every model on the platform must
#      implement load()/get_move(state, fen, player).
# ──────────────────────────────────────────────
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from django.conf import settings

from apps.games.exceptions import SandboxUnavailableError

log = logging.getLogger(__name__)

# Shared by both in-container scripts below. Kept dependency-free (stdlib +
# numpy/python-chess only) since it runs inside the minimal sandbox image.
# Paths are read from AGL_MODEL_DIR / AGL_DATA_DIR so this same source can
# be exercised outside Docker (unit tests) by pointing those env vars at a
# temp directory; in production the container never sets them, so they
# default to the real bind-mount paths /model and /data.
_COMMON_PRELUDE = '''
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

MODEL_DIR = Path(os.environ.get("AGL_MODEL_DIR", "/model"))
_data_dir_env = os.environ.get("AGL_DATA_DIR", "/data")
DATA_DIR = Path(_data_dir_env) if Path(_data_dir_env).is_dir() else None

_NOTES = []
_ENCODINGS = ("utf-8-sig", "cp1255", "cp1252", "latin-1")


def _decode_bytes(data: bytes, label: str) -> str:
    for i, enc in enumerate(_ENCODINGS):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if i > 0:
            note = label + ": not valid UTF-8, decoded as " + enc
            if note not in _NOTES:
                _NOTES.append(note)
        return text
    return data.decode("latin-1", errors="replace")


class _TolerantLoader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *args, **kwargs):
        if isinstance(data, (bytes, bytearray)):
            data = _decode_bytes(bytes(data), Path(str(path)).name)
        return compile(data, str(path), "exec", dont_inherit=True)


def _install_import_hook() -> None:
    # Makes `import helper` inside the model dir tolerant too.
    root = str(MODEL_DIR)
    factory = importlib.machinery.FileFinder.path_hook(
        (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
        (_TolerantLoader, importlib.machinery.SOURCE_SUFFIXES),
        (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
    )

    def hook(path):
        p = os.path.abspath(path)
        if p != root and not p.startswith(root + os.sep):
            raise ImportError
        return factory(path)

    sys.path_hooks.insert(0, hook)
    sys.path_importer_cache.clear()


def _load_manifest() -> dict:
    """Read config_model.json. Missing/absent fields are NOT filled in here
    (defaults are applied by the caller) so "the field was absent" stays
    distinguishable from "the field was explicitly empty"."""
    cfg_path = MODEL_DIR / "config_model.json"
    if cfg_path.exists():
        return json.loads(_decode_bytes(cfg_path.read_bytes(), "config_model.json"))
    return {}


def _entrypoint_candidates(manifest: dict, game_type: str) -> list:
    """Resolve which .py file(s) to import, per the manifest contract."""
    modules = manifest.get("modules")
    if modules:
        return list(modules)
    entrypoint = manifest.get("entrypoint")
    if entrypoint:
        return [entrypoint]
    default_entrypoint = (game_type or "chess") + "_mcvs.py"
    if (MODEL_DIR / default_entrypoint).exists():
        return [default_entrypoint]
    # Legacy fallback: import every *.py in /model.
    return sorted(
        p.name for p in MODEL_DIR.glob("*.py") if not p.name.startswith("_agl_")
    )


def _load_modules(manifest: dict, game_type: str):
    loaded = None
    for fname in _entrypoint_candidates(manifest, game_type):
        mpath = MODEL_DIR / fname
        if not mpath.exists():
            continue
        mod_name = Path(fname).stem
        loader = _TolerantLoader(mod_name, str(mpath))
        spec = importlib.util.spec_from_file_location(mod_name, str(mpath), loader=loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        loaded = mod
    return loaded


class _Context:
    """Passed to the new-style ``load(ctx)`` entrypoint."""
    def __init__(self, model_dir, data_dir, game_type):
        self.model_dir = model_dir
        self.data_dir = data_dir
        self.game_type = game_type
'''

# Written into the container as /runner/run_predict.py.
_MOVE_MAIN = '''

def _fail(msg: str) -> None:
    print(json.dumps({"error": msg, "warnings": _NOTES}))
    sys.exit(1)


def main() -> None:
    fen = os.environ.get("AGL_FEN", "")
    player = os.environ.get("AGL_PLAYER", "w")
    game_type = os.environ.get("AGL_GAME_TYPE", "chess")

    _install_import_hook()
    sys.path.insert(0, str(MODEL_DIR))

    try:
        manifest = _load_manifest()
    except Exception as exc:
        _fail(f"config_model.json is invalid: {exc}")
        return

    try:
        module = _load_modules(manifest, game_type)
    except Exception as exc:
        _fail(f"module load failed: {exc}")
        return

    if module is None:
        _fail("no model module found in /model")
        return

    if not hasattr(module, "load"):
        _fail("module has no load(ctx) entrypoint — every model must implement load(ctx)/get_move(state, fen, player)")
        return

    move = None
    try:
        ctx = _Context(MODEL_DIR, DATA_DIR, game_type)
        state = module.load(ctx)
        move = module.get_move(state, fen, player)
    except Exception as exc:
        _fail(f"prediction failed: {exc}")
        return

    if not move or not isinstance(move, str):
        _fail("model returned no move")
        return

    print(json.dumps({"move": move, "warnings": _NOTES}))


if __name__ == "__main__":
    main()
'''

_RUNNER_SCRIPT = _COMMON_PRELUDE + _MOVE_MAIN


# Written into the container as /runner/run_check.py by run_check_in_sandbox().
# Unlike _MOVE_MAIN, this NEVER sys.exit(1)s on a model-quality problem — it
# collects every problem it finds and always prints a single JSON summary, so
# apps.games.model_check.check_model() gets the full list in one pass instead
# of stopping at the first failure.
_CHECK_MAIN = '''
import time

# ── Self-contained legality checkers (no Django import inside the sandbox) ──

def _chess_sample_positions():
    return [
        ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "w"),
        ("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3", "w"),
        ("8/8/8/3k4/8/3K4/8/7R w - - 0 1", "w"),
    ]


def _chess_is_legal(fen, uci):
    import chess
    board = chess.Board(fen)
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return False
    return move in board.legal_moves


def _breakthrough_sample_positions():
    return [
        ("BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w", "w"),
        ("BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW b", "b"),
        ("8/8/2B1B3/8/3W4/8/8/8 w", "w"),
    ]


_BT_FILES = "abcdefgh"
_BT_RANKS = "12345678"


def _breakthrough_grid(fen):
    parts = fen.strip().split()
    ranks = parts[0].split("/")
    grid = []
    for rank_str in ranks:
        row = []
        for ch in rank_str:
            if ch.isdigit():
                row.extend(["."] * int(ch))
            else:
                row.append(ch)
        grid.append(row)
    return grid


def _breakthrough_sq(sq):
    file_idx = _BT_FILES.index(sq[0])
    rank_idx = int(sq[1]) - 1
    row = 7 - rank_idx
    return row, file_idx


def _breakthrough_legal_moves(fen):
    grid = _breakthrough_grid(fen)
    turn = fen.strip().split()[1] if len(fen.strip().split()) > 1 else "w"
    piece = "W" if turn == "w" else "B"
    direction = -1 if turn == "w" else 1
    moves = []
    for r in range(8):
        for c in range(8):
            if grid[r][c] != piece:
                continue
            nr = r + direction
            if nr < 0 or nr >= 8:
                continue
            if grid[nr][c] == ".":
                moves.append(_BT_FILES[c] + _BT_RANKS[7 - r] + _BT_FILES[c] + _BT_RANKS[7 - nr])
            for dc in (-1, 1):
                nc = c + dc
                if nc < 0 or nc >= 8:
                    continue
                target = grid[nr][nc]
                if target == piece:
                    continue
                moves.append(_BT_FILES[c] + _BT_RANKS[7 - r] + _BT_FILES[nc] + _BT_RANKS[7 - nr])
    return moves


def _breakthrough_is_legal(fen, uci):
    return isinstance(uci, str) and len(uci) == 4 and uci in _breakthrough_legal_moves(fen)


def _sample_positions(game_type):
    if game_type == "breakthrough":
        return _breakthrough_sample_positions()
    return _chess_sample_positions()


def _is_legal(game_type, fen, uci):
    if game_type == "breakthrough":
        return _breakthrough_is_legal(fen, uci)
    return _chess_is_legal(fen, uci)


def main() -> None:
    game_type = os.environ.get("AGL_GAME_TYPE", "chess")
    problems = []

    try:
        manifest = _load_manifest()
    except Exception as exc:
        problems.append(f"config_model.json is invalid JSON: {exc}")
        manifest = {}

    if "schema_version" in manifest and not isinstance(manifest["schema_version"], int):
        problems.append("manifest field 'schema_version' must be an integer")

    for field in ("data_files", "modules", "requirements"):
        if field in manifest and not isinstance(manifest[field], list):
            problems.append(f"manifest field '{field}' must be a list")

    for fname in manifest.get("data_files") or []:
        if DATA_DIR is None or not (DATA_DIR / fname).exists():
            problems.append(f"data file '{fname}' listed in manifest but not found in the data folder")

    _install_import_hook()
    sys.path.insert(0, str(MODEL_DIR))

    module = None
    try:
        module = _load_modules(manifest, game_type)
    except Exception as exc:
        problems.append(f"module import failed: {exc}")

    if module is None and not problems:
        problems.append("no model module found (check 'entrypoint'/'modules' in config_model.json)")

    state = None
    if module is not None:
        if not hasattr(module, "load"):
            problems.append("module has no load(ctx) entrypoint — every model must implement load(ctx)/get_move(state, fen, player)")
            module = None
        else:
            try:
                ctx = _Context(MODEL_DIR, DATA_DIR, game_type)
                state = module.load(ctx)
            except Exception as exc:
                problems.append(f"load(ctx) raised an exception: {exc}")
                module = None  # can't run get_move without state

    moves = []
    if module is not None:
        for i, (fen, player) in enumerate(_sample_positions(game_type), start=1):
            t0 = time.monotonic()
            move = None
            try:
                move = module.get_move(state, fen, player)
            except Exception as exc:
                problems.append(f"sample position {i} raised an exception: {exc}")
                moves.append({"position": i, "fen": fen, "player": player, "move": None, "error": str(exc)})
                continue
            elapsed = time.monotonic() - t0

            if not move or not isinstance(move, str):
                problems.append(f"sample position {i}: model returned no move")
                moves.append({"position": i, "fen": fen, "player": player, "move": None})
                continue
            legal = _is_legal(game_type, fen, move)
            if not legal:
                problems.append(f"sample position {i}: '{move}' is not a legal move for this position")
            moves.append({"position": i, "fen": fen, "player": player, "move": move, "legal": legal})
            if elapsed > 15:
                _NOTES.append(f"sample position {i} took {elapsed:.1f}s (slow)")

    print(json.dumps({"problems": problems, "warnings": _NOTES, "moves": moves}))


if __name__ == "__main__":
    main()
'''

_CHECK_RUNNER_SCRIPT = _COMMON_PRELUDE + _CHECK_MAIN


def _docker_client():
    """Return a connected docker-py client, or raise SandboxUnavailableError."""
    try:
        import docker
    except ImportError as exc:
        raise SandboxUnavailableError(
            "The 'docker' package is not installed — Docker sandbox is mandatory."
        ) from exc

    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        raise SandboxUnavailableError(f"Docker daemon unavailable: {exc}") from exc
    return client


def run_predict_in_sandbox(
    model_dir: Path,
    data_dir: Path | None,
    fen: str,
    player: str,
    game_type: str,
    *,
    timeout: int | None = None,
) -> str | None:
    """Run the user's model inside an isolated Docker container and return a move.

    Raises ``SandboxUnavailableError`` on infrastructure faults (Docker daemon
    unreachable, sandbox image missing, Docker API errors) — callers MUST let
    this propagate; there is no local fallback.

    Returns the move string on success, or ``None`` if the container ran to
    completion but the model produced no valid move (a model-quality issue,
    not an infra fault — callers may fall back to a random legal move).
    """
    from docker.errors import APIError, ImageNotFound, NotFound
    import requests as _requests

    timeout = timeout or int(getattr(settings, "SANDBOX_MOVE_TIMEOUT", 30))
    image = getattr(settings, "SANDBOX_DOCKER_IMAGE", "python:3.11-slim")
    mem_limit_mb = int(getattr(settings, "SANDBOX_MEMORY_LIMIT_MB", 512))
    nano_cpus = int(float(getattr(settings, "SANDBOX_CPU_LIMIT", 1.0)) * 1_000_000_000)
    pids_limit = int(getattr(settings, "SANDBOX_PIDS_LIMIT", 128))

    client = _docker_client()

    exit_code = 1
    logs = ""

    with tempfile.TemporaryDirectory(prefix="agl_sandbox_runner_") as runner_tmp:
        runner_dir = Path(runner_tmp)
        (runner_dir / "run_predict.py").write_text(_RUNNER_SCRIPT, encoding="utf-8")

        volumes = {
            str(model_dir.resolve()): {"bind": "/model", "mode": "ro"},
            str(runner_dir.resolve()): {"bind": "/runner", "mode": "ro"},
        }
        if data_dir is not None:
            volumes[str(data_dir.resolve())] = {"bind": "/data", "mode": "ro"}

        container = None
        try:
            try:
                container = client.containers.create(
                    image=image,
                    command=["python", "/runner/run_predict.py"],
                    environment={
                        "AGL_FEN": fen,
                        "AGL_PLAYER": player,
                        "AGL_GAME_TYPE": game_type,
                        "PYTHONUTF8": "1",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    volumes=volumes,
                    network_disabled=True,
                    mem_limit=f"{mem_limit_mb}m",
                    nano_cpus=nano_cpus,
                    pids_limit=pids_limit,
                    read_only=True,
                    tmpfs={"/tmp": "size=64m"},
                    user="nobody",
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                )
            except (ImageNotFound, NotFound) as exc:
                raise SandboxUnavailableError(
                    f"Sandbox image '{image}' not found: {exc}"
                ) from exc
            except APIError as exc:
                raise SandboxUnavailableError(f"Docker API error: {exc}") from exc

            container.start()
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", 1)
            except (_requests.exceptions.ReadTimeout, _requests.exceptions.ConnectionError):
                log.warning(
                    "Sandbox move timed out after %ss (game_type=%s)", timeout, game_type,
                )
                try:
                    container.kill()
                except Exception:
                    pass
                return None

            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        except APIError as exc:
            raise SandboxUnavailableError(f"Docker daemon error: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    log.debug("Could not remove sandbox container", exc_info=True)

    if exit_code != 0:
        log.warning(
            "Sandbox exited %s for game_type=%s: %s",
            exit_code, game_type, logs.strip()[-1500:],
        )
        return None

    lines = [ln for ln in logs.strip().splitlines() if ln.strip()]
    if not lines:
        log.warning("Sandbox produced no output for game_type=%s", game_type)
        return None

    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        log.warning("Sandbox produced non-JSON output for game_type=%s: %r", game_type, logs[-500:])
        return None

    for note in data.get("warnings") or []:
        log.warning("Sandbox encoding notice (game_type=%s): %s", game_type, note)

    if "error" in data:
        log.warning("Sandbox model error for game_type=%s: %s", game_type, data["error"])
        return None

    move = data.get("move")
    return move if isinstance(move, str) and move.strip() else None


def run_check_in_sandbox(
    model_dir: Path,
    data_dir: Path | None,
    game_type: str,
    *,
    timeout: int | None = None,
) -> dict:
    """Run apps.games.model_check's contract test suite inside the sandbox.

    Same hardening as run_predict_in_sandbox() (no network, read-only,
    non-root, resource limits). Never raises for model-quality problems —
    everything the model gets wrong is returned as a human-readable string
    in the "problems" list. Only infra faults raise SandboxUnavailableError.

    Returns {"problems": [str, ...], "warnings": [str, ...]}.
    An empty "problems" list means the model passed every check.
    """
    from docker.errors import APIError, ImageNotFound, NotFound
    import requests as _requests

    timeout = timeout or int(getattr(settings, "SANDBOX_VERIFY_TIMEOUT", 300))
    image = getattr(settings, "SANDBOX_DOCKER_IMAGE", "python:3.11-slim")
    mem_limit_mb = int(getattr(settings, "SANDBOX_MEMORY_LIMIT_MB", 512))
    nano_cpus = int(float(getattr(settings, "SANDBOX_CPU_LIMIT", 1.0)) * 1_000_000_000)
    pids_limit = int(getattr(settings, "SANDBOX_PIDS_LIMIT", 128))

    client = _docker_client()

    exit_code = 1
    logs = ""

    with tempfile.TemporaryDirectory(prefix="agl_sandbox_check_") as runner_tmp:
        runner_dir = Path(runner_tmp)
        (runner_dir / "run_check.py").write_text(_CHECK_RUNNER_SCRIPT, encoding="utf-8")

        volumes = {
            str(model_dir.resolve()): {"bind": "/model", "mode": "ro"},
            str(runner_dir.resolve()): {"bind": "/runner", "mode": "ro"},
        }
        if data_dir is not None:
            volumes[str(data_dir.resolve())] = {"bind": "/data", "mode": "ro"}

        container = None
        try:
            try:
                container = client.containers.create(
                    image=image,
                    command=["python", "/runner/run_check.py"],
                    environment={
                        "AGL_GAME_TYPE": game_type,
                        "PYTHONUTF8": "1",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    volumes=volumes,
                    network_disabled=True,
                    mem_limit=f"{mem_limit_mb}m",
                    nano_cpus=nano_cpus,
                    pids_limit=pids_limit,
                    read_only=True,
                    tmpfs={"/tmp": "size=64m"},
                    user="nobody",
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                )
            except (ImageNotFound, NotFound) as exc:
                raise SandboxUnavailableError(
                    f"Sandbox image '{image}' not found: {exc}"
                ) from exc
            except APIError as exc:
                raise SandboxUnavailableError(f"Docker API error: {exc}") from exc

            container.start()
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", 1)
            except (_requests.exceptions.ReadTimeout, _requests.exceptions.ConnectionError):
                log.warning(
                    "Sandbox check timed out after %ss (game_type=%s)", timeout, game_type,
                )
                try:
                    container.kill()
                except Exception:
                    pass
                return {
                    "problems": [f"model check exceeded the {timeout}s time limit"],
                    "warnings": [],
                }

            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        except APIError as exc:
            raise SandboxUnavailableError(f"Docker daemon error: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    log.debug("Could not remove sandbox container", exc_info=True)

    if exit_code != 0:
        log.warning(
            "Sandbox check exited %s for game_type=%s: %s",
            exit_code, game_type, logs.strip()[-1500:],
        )
        return {
            "problems": [
                "sandbox exited abnormally while checking the model "
                "(this can mean a crash or exceeding the memory limit): "
                + logs.strip()[-500:]
            ],
            "warnings": [],
        }

    lines = [ln for ln in logs.strip().splitlines() if ln.strip()]
    if not lines:
        return {"problems": ["sandbox produced no output while checking the model"], "warnings": []}

    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {
            "problems": ["sandbox produced non-JSON output while checking the model: " + logs[-500:]],
            "warnings": [],
        }

    return {
        "problems": list(data.get("problems") or []),
        "warnings": list(data.get("warnings") or []),
        "moves": list(data.get("moves") or []),
    }