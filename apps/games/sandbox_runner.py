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

import atexit
import json
import logging
import os
import shutil
import tempfile
import threading
import time
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
    import time as _tm
    _t_start = _tm.monotonic()
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
        _t_imported = _tm.monotonic()
        ctx = _Context(MODEL_DIR, DATA_DIR, game_type)
        state = module.load(ctx)
        _t_loaded = _tm.monotonic()
        move = module.get_move(state, fen, player)
        _t_moved = _tm.monotonic()
    except Exception as exc:
        _fail(f"prediction failed: {exc}")
        return

    if not move or not isinstance(move, str):
        _fail("model returned no move")
        return

    _timing = {
        "import_model_s": round(_t_imported - _t_start, 3),
        "load_s": round(_t_loaded - _t_imported, 3),
        "get_move_s": round(_t_moved - _t_loaded, 3),
    }
    print(json.dumps({"move": move, "warnings": _NOTES, "timing": _timing}))


if __name__ == "__main__":
    main()
'''

_RUNNER_SCRIPT = _COMMON_PRELUDE + _MOVE_MAIN

# Written into the container as /runner/run_worker.py by _start_worker().
# Loads the model ONCE, then answers many moves over a tiny file-based protocol
# on the /ipc bind-mount (the container has no network, so no sockets):
#   host writes  /ipc/request.json   {"seq": n, "fen": ..., "player": ...}
#   worker writes /ipc/response.json {"seq": n, "move": ...} or {"seq": n, "error": ...}
# /ipc/ready.json is written once after load() finished (or failed).
_WORKER_MAIN = '''

def main() -> None:
    import time as _tm
    game_type = os.environ.get("AGL_GAME_TYPE", "chess")
    ipc = Path(os.environ.get("AGL_IPC_DIR", "/ipc"))
    idle_limit = float(os.environ.get("AGL_WORKER_IDLE_SECONDS", "300"))

    def write(name, obj):
        tmp = ipc / (name + ".tmp")
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        os.replace(str(tmp), str(ipc / name))

    t0 = _tm.monotonic()
    _install_import_hook()
    sys.path.insert(0, str(MODEL_DIR))
    try:
        manifest = _load_manifest()
        module = _load_modules(manifest, game_type)
        if module is None:
            raise RuntimeError("no model module found in /model")
        if not hasattr(module, "load"):
            raise RuntimeError("module has no load(ctx) entrypoint")
        state = module.load(_Context(MODEL_DIR, DATA_DIR, game_type))
    except BaseException as exc:
        write("ready.json", {"error": "worker init failed: " + str(exc), "warnings": _NOTES})
        sys.exit(1)
    write("ready.json", {"ok": True, "warnings": _NOTES, "init_s": round(_tm.monotonic() - t0, 3)})

    last_activity = _tm.monotonic()
    req_path = ipc / "request.json"
    while True:
        if not req_path.exists():
            if _tm.monotonic() - last_activity > idle_limit:
                sys.exit(0)
            _tm.sleep(0.005)
            continue
        try:
            data = json.loads(req_path.read_text(encoding="utf-8"))
            req_path.unlink()
        except Exception:
            _tm.sleep(0.005)
            continue
        last_activity = _tm.monotonic()
        seq = data.get("seq")
        t1 = _tm.monotonic()
        try:
            move = module.get_move(state, data.get("fen", ""), data.get("player", "w"))
            if not move or not isinstance(move, str):
                resp = {"seq": seq, "error": "model returned no move"}
            else:
                resp = {"seq": seq, "move": move, "get_move_s": round(_tm.monotonic() - t1, 3)}
        except Exception as exc:
            resp = {"seq": seq, "error": "prediction failed: " + str(exc)}
        resp["warnings"] = _NOTES
        write("response.json", resp)
        last_activity = _tm.monotonic()


if __name__ == "__main__":
    main()
'''

_WORKER_SCRIPT = _COMMON_PRELUDE + _WORKER_MAIN



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


def _run_predict_one_shot(
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

    _t_begin = time.monotonic()
    client = _docker_client()
    _t_client = time.monotonic()
    _t_created = _t_waited = None

    exit_code = 1
    logs = ""

    with tempfile.TemporaryDirectory(prefix="agl_sandbox_runner_") as runner_tmp:
        runner_dir = Path(runner_tmp)
        os.chmod(runner_dir, 0o755)
        (runner_dir / "run_predict.py").write_text(_RUNNER_SCRIPT, encoding="utf-8")

        volumes = {
            str(_real_model_dir(model_dir)): {"bind": "/model", "mode": "ro"},
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

            _t_created = time.monotonic()
            container.start()
            try:
                result = container.wait(timeout=timeout)
                _t_waited = time.monotonic()
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

    _t_removed = time.monotonic()

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

    # ── Timing breakdown: shows WHERE the per-move seconds go ──
    try:
        _ct = data.get("timing") or {}
        _created = _t_created if _t_created is not None else _t_client
        _waited = _t_waited if _t_waited is not None else _t_removed
        _run = _waited - _created
        _inside = sum(_ct.get(k, 0.0) for k in ("import_model_s", "load_s", "get_move_s"))
        log.info(
            "[sandbox-timing] game_type=%s total=%.2fs | docker_client=%.2fs create=%.2fs "
            "container_run=%.2fs (python+docker start-up=%.2fs, import model/torch=%.2fs, "
            "load()=%.2fs, get_move()=%.2fs) | cleanup=%.2fs",
            game_type, _t_removed - _t_begin,
            _t_client - _t_begin, _created - _t_client,
            _run, max(0.0, _run - _inside),
            _ct.get("import_model_s", -1.0), _ct.get("load_s", -1.0), _ct.get("get_move_s", -1.0),
            _t_removed - _waited,
        )
    except Exception:
        log.debug("could not log sandbox timing", exc_info=True)

    move = data.get("move")
    return move if isinstance(move, str) and move.strip() else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Warm worker pool
#
#  Starting a fresh container per move costs ~4-5 s (container start +
#  importing torch + model load()), which is far more than the model's real
#  thinking time. Instead we keep ONE container per model (same hardening as
#  the one-shot path) running a worker that load()s once and then answers
#  moves. Workers are reaped after SANDBOX_WORKER_IDLE_SECONDS of inactivity
#  and restarted automatically if the model files change, the container dies,
#  or a move times out.
#
#  Settings (all optional):
#    SANDBOX_REUSE_CONTAINERS      bool  default True  (False = old one-container-per-move)
#    SANDBOX_WORKER_IDLE_SECONDS   float default 900
#    SANDBOX_WORKER_START_TIMEOUT  int   default 120   (cold start: import + load)
#    SANDBOX_MAX_WORKERS           int   default 8
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_workers: dict = {}
_key_locks: dict = {}
_pool_lock = threading.Lock()
_reaper_started = False


def _key_lock(base) -> threading.Lock:
    with _pool_lock:
        lock = _key_locks.get(base)
        if lock is None:
            lock = _key_locks[base] = threading.Lock()
        return lock


def _fingerprint(path) -> int | None:
    """Cheap change detector for a model/data dir (relative name + size + mtime)."""
    if path is None:
        return None
    items = []
    for dirpath, _dirs, files in os.walk(str(path)):
        for fname in files:
            p = os.path.join(dirpath, fname)
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append((os.path.relpath(p, str(path)), st.st_size, st.st_mtime_ns))
        if len(items) > 5000:
            break
    return hash(tuple(sorted(items)))


class _Worker:
    def __init__(self, base, fp, container, root: Path):
        self.base = base
        self.fp = fp
        self.container = container
        self.root = root
        self.ipc_dir = root / "ipc"
        self.seq = 0
        self.last_used = time.monotonic()

    def alive(self) -> bool:
        try:
            self.container.reload()
            return self.container.status == "running"
        except Exception:
            return False

    def stop(self) -> None:
        try:
            self.container.kill()
        except Exception:
            pass
        try:
            self.container.remove(force=True)
        except Exception:
            log.debug("Could not remove sandbox worker container", exc_info=True)
        shutil.rmtree(str(self.root), ignore_errors=True)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _wait_for_json(path: Path, timeout: float, container):
    """Poll for *path*. Returns (data, reason) where reason is 'ok'|'timeout'|'died'."""
    deadline = time.monotonic() + timeout
    next_alive_check = time.monotonic() + 0.5
    while True:
        if path.exists():
            data = _read_json(path)
            if data is not None:
                return data, "ok"
        now = time.monotonic()
        if now > deadline:
            return None, "timeout"
        if now >= next_alive_check:
            try:
                container.reload()
                if container.status != "running":
                    # one last look: it may have written the file just before exiting
                    data = _read_json(path) if path.exists() else None
                    return (data, "ok") if data is not None else (None, "died")
            except Exception:
                return None, "died"
            next_alive_check = now + 0.5
        time.sleep(0.005)


def _start_worker(base, fp, model_dir: Path, data_dir: Path | None, game_type: str):
    """Create + start a worker container and wait until load() finished.

    Returns a ready _Worker, or None if the model failed to initialise.
    Raises SandboxUnavailableError on infrastructure faults.
    """
    from docker.errors import APIError, ImageNotFound, NotFound

    image = getattr(settings, "SANDBOX_DOCKER_IMAGE", "python:3.11-slim")
    mem_limit_mb = int(getattr(settings, "SANDBOX_MEMORY_LIMIT_MB", 512))
    nano_cpus = int(float(getattr(settings, "SANDBOX_CPU_LIMIT", 1.0)) * 1_000_000_000)
    pids_limit = int(getattr(settings, "SANDBOX_PIDS_LIMIT", 128))
    idle_s = float(getattr(settings, "SANDBOX_WORKER_IDLE_SECONDS", 900))
    start_timeout = float(getattr(settings, "SANDBOX_WORKER_START_TIMEOUT", 120))

    t0 = time.monotonic()
    client = _docker_client()
    t_client = time.monotonic()

    root = Path(tempfile.mkdtemp(prefix="agl_sandbox_worker_"))
    runner_dir = root / "runner"
    ipc_dir = root / "ipc"
    runner_dir.mkdir()
    ipc_dir.mkdir()
    try:
        os.chmod(str(ipc_dir), 0o777)  # the container runs as 'nobody'
    except OSError:
        pass
    (runner_dir / "run_worker.py").write_text(_WORKER_SCRIPT, encoding="utf-8")
    t_prep = time.monotonic()

    volumes = {
        str(_real_model_dir(model_dir)): {"bind": "/model", "mode": "ro"},
        str(runner_dir.resolve()): {"bind": "/runner", "mode": "ro"},
        str(ipc_dir.resolve()): {"bind": "/ipc", "mode": "rw"},
    }
    if data_dir is not None:
        volumes[str(data_dir.resolve())] = {"bind": "/data", "mode": "ro"}

    container = None
    try:
        try:
            container = client.containers.create(
                image=image,
                command=["python", "/runner/run_worker.py"],
                environment={
                    "AGL_GAME_TYPE": game_type,
                    "AGL_IPC_DIR": "/ipc",
                    "AGL_WORKER_IDLE_SECONDS": str(int(idle_s) + 180),  # self-exit if host vanished
                    "PYTHONUTF8": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                labels={"agl.sandbox.worker": "1"},
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
            raise SandboxUnavailableError(f"Sandbox image '{image}' not found: {exc}") from exc
        except APIError as exc:
            raise SandboxUnavailableError(f"Docker API error: {exc}") from exc

        t_created = time.monotonic()
        try:
            container.start()
        except APIError as exc:
            raise SandboxUnavailableError(f"Docker daemon error: {exc}") from exc

        t_started = time.monotonic()
        ready, reason = _wait_for_json(ipc_dir / "ready.json", start_timeout, container)
        t_ready = time.monotonic()
    except BaseException:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        shutil.rmtree(str(root), ignore_errors=True)
        raise

    worker = _Worker(base, fp, container, root)
    if ready is None or "error" in ready:
        logs = ""
        try:
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")[-1500:]
        except Exception:
            pass
        log.warning(
            "Sandbox worker failed to start (game_type=%s, %s): %s %s",
            game_type, reason, (ready or {}).get("error", ""), logs.strip(),
        )
        worker.stop()
        return None

    for note in ready.get("warnings") or []:
        log.warning("Sandbox encoding notice (game_type=%s): %s", game_type, note)
    init_s = ready.get("init_s", -1.0)
    wait_s = t_ready - t_started
    log.info(
        "[sandbox-timing] worker started game_type=%s in %.2fs | docker_client=%.2fs prep=%.2fs "
        "create=%.2fs start()=%.2fs wait_for_ready=%.2fs (of which model init inside container=%.2fs, "
        "interpreter start-up + file-sync lag=%.2fs)",
        game_type, time.monotonic() - t0,
        t_client - t0, t_prep - t_client, t_created - t_prep, t_started - t_created,
        wait_s, init_s, max(0.0, wait_s - init_s),
    )
    return worker


def _worker_request(worker: _Worker, fen: str, player: str, timeout: float):
    """Send one move request. Returns (response_dict | None, reason)."""
    worker.seq += 1
    seq = worker.seq
    resp_path = worker.ipc_dir / "response.json"
    try:
        resp_path.unlink()
    except OSError:
        pass
    tmp = worker.ipc_dir / "request.tmp"
    tmp.write_text(json.dumps({"seq": seq, "fen": fen, "player": player}), encoding="utf-8")
    os.replace(str(tmp), str(worker.ipc_dir / "request.json"))

    deadline = time.monotonic() + timeout
    next_alive_check = time.monotonic() + 0.5
    while True:
        if resp_path.exists():
            data = _read_json(resp_path)
            if data is not None and data.get("seq") == seq:
                return data, "ok"
        now = time.monotonic()
        if now > deadline:
            return None, "timeout"
        if now >= next_alive_check:
            if not worker.alive():
                return None, "died"
            next_alive_check = now + 0.5
        time.sleep(0.005)


def _evict_one(exclude_base) -> None:
    """Free a slot by stopping the least-recently-used idle worker."""
    with _pool_lock:
        candidates = sorted(
            (w for b, w in _workers.items() if b != exclude_base),
            key=lambda w: w.last_used,
        )
    for w in candidates:
        lock = _key_lock(w.base)
        if lock.acquire(blocking=False):
            try:
                with _pool_lock:
                    if _workers.get(w.base) is w:
                        _workers.pop(w.base, None)
                    else:
                        continue
                w.stop()
                return
            finally:
                lock.release()


def _reaper_loop() -> None:
    while True:
        time.sleep(15)
        try:
            idle = float(getattr(settings, "SANDBOX_WORKER_IDLE_SECONDS", 900))
            now = time.monotonic()
            with _pool_lock:
                items = list(_workers.items())
            for base, w in items:
                if now - w.last_used <= idle:
                    continue
                lock = _key_lock(base)
                if lock.acquire(blocking=False):
                    try:
                        if time.monotonic() - w.last_used > idle:
                            with _pool_lock:
                                if _workers.get(base) is w:
                                    _workers.pop(base, None)
                                else:
                                    continue
                            log.info("[sandbox-timing] stopping idle worker for %s", base[0])
                            w.stop()
                    finally:
                        lock.release()
        except Exception:
            log.debug("sandbox worker reaper error", exc_info=True)


def _ensure_reaper() -> None:
    global _reaper_started
    with _pool_lock:
        if _reaper_started:
            return
        _reaper_started = True
    threading.Thread(target=_reaper_loop, name="agl-sandbox-reaper", daemon=True).start()
    atexit.register(_stop_all_workers)


def _stop_all_workers() -> None:
    with _pool_lock:
        workers = list(_workers.values())
        _workers.clear()
    for w in workers:
        try:
            w.stop()
        except Exception:
            pass


def _ensure_worker_locked(base, fp, model_dir, data_dir, game_type):
    """Return ``(worker | None, cold)``. The caller MUST hold ``_key_lock(base)``.

    Reuses the running worker when it is alive and the model files are unchanged;
    otherwise (re)starts it. ``None`` means the model failed to initialise.
    """
    with _pool_lock:
        worker = _workers.get(base)
    if worker is not None and (worker.fp != fp or not worker.alive()):
        log.info("[sandbox-timing] restarting worker (model changed or container died)")
        with _pool_lock:
            _workers.pop(base, None)
        worker.stop()
        worker = None
    if worker is not None:
        return worker, False

    max_workers = int(getattr(settings, "SANDBOX_MAX_WORKERS", 8))
    with _pool_lock:
        full = len(_workers) >= max_workers
    if full:
        _evict_one(base)
    worker = _start_worker(base, fp, model_dir, data_dir, game_type)
    if worker is None:
        return None, True
    with _pool_lock:
        _workers[base] = worker
    return worker, True


def prewarm_worker(model_dir: Path, data_dir: Path | None, game_type: str) -> bool:
    """Start (or confirm) the warm worker for this model WITHOUT asking for a move.

    Call this before a game starts so the first move does not pay the cold start
    (container start + torch import + model load()). Returns True when a warm
    worker is ready. Never raises for model problems; a real move request will
    surface those properly. No-op (False) when container reuse is disabled.
    """
    if not getattr(settings, "SANDBOX_REUSE_CONTAINERS", True):
        return False
    base = (
        str(model_dir.resolve()),
        str(data_dir.resolve()) if data_dir is not None else None,
        game_type,
    )
    fp = (_fingerprint(model_dir), _fingerprint(data_dir))
    _ensure_reaper()
    with _key_lock(base):
        worker, _cold = _ensure_worker_locked(base, fp, model_dir, data_dir, game_type)
        if worker is None:
            return False
        worker.last_used = time.monotonic()  # restart the idle timer
        return True


def _run_predict_pooled(model_dir, data_dir, fen, player, game_type, timeout) -> str | None:
    t_begin = time.monotonic()
    base = (
        str(model_dir.resolve()),
        str(data_dir.resolve()) if data_dir is not None else None,
        game_type,
    )
    fp = (_fingerprint(model_dir), _fingerprint(data_dir))
    _ensure_reaper()

    with _key_lock(base):
        worker, cold = _ensure_worker_locked(base, fp, model_dir, data_dir, game_type)
        if worker is None:
            return None

        resp, reason = _worker_request(worker, fen, player, timeout)
        worker.last_used = time.monotonic()

        if resp is None:
            log.warning(
                "Sandbox worker %s for game_type=%s (timeout=%ss) - restarting it next move",
                reason, game_type, timeout,
            )
            with _pool_lock:
                if _workers.get(base) is worker:
                    _workers.pop(base, None)
            worker.stop()
            return None

    for note in resp.get("warnings") or []:
        log.warning("Sandbox encoding notice (game_type=%s): %s", game_type, note)
    if "error" in resp:
        log.warning("Sandbox model error for game_type=%s: %s", game_type, resp["error"])
        return None

    log.info(
        "[sandbox-timing] game_type=%s %s total=%.2fs | get_move()=%.2fs",
        game_type, "COLD" if cold else "warm",
        time.monotonic() - t_begin, resp.get("get_move_s", -1.0),
    )
    move = resp.get("move")
    return move if isinstance(move, str) and move.strip() else None


def run_predict_in_sandbox(
    model_dir: Path,
    data_dir: Path | None,
    fen: str,
    player: str,
    game_type: str,
    *,
    timeout: int | None = None,
) -> str | None:
    """Return a move from the user's model, running it inside the Docker sandbox.

    By default a warm per-model worker container is reused between moves (the
    model is loaded once). Set ``SANDBOX_REUSE_CONTAINERS = False`` to get the
    old behaviour of one fresh container per move.

    Raises ``SandboxUnavailableError`` on infrastructure faults. Returns ``None``
    if the model produced no valid move (timeout, crash, bad output).
    """
    if not getattr(settings, "SANDBOX_REUSE_CONTAINERS", True):
        return _run_predict_one_shot(model_dir, data_dir, fen, player, game_type, timeout=timeout)
    timeout = timeout or int(getattr(settings, "SANDBOX_MOVE_TIMEOUT", 30))
    return _run_predict_pooled(model_dir, data_dir, fen, player, game_type, timeout)


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
        os.chmod(runner_dir, 0o755)
        (runner_dir / "run_check.py").write_text(_CHECK_RUNNER_SCRIPT, encoding="utf-8")

        volumes = {
            str(_real_model_dir(model_dir)): {"bind": "/model", "mode": "ro"},
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


def _real_model_dir(model_dir):
    """Return a directory with real files (symlinks followed) for mounting.

    HF cache snapshots are symlinks into ../../blobs, which do not exist inside
    the container. Build one real-file copy per distinct content and reuse it.
    Directories without symlinks are returned unchanged.
    """
    import hashlib
    from django.conf import settings

    src = Path(model_dir).resolve()
    if not any(f.is_symlink() for f in src.rglob("*")):
        return src

    sig = hashlib.sha1()
    for f in sorted(src.rglob("*")):
        if f.is_file():
            st = f.stat()
            sig.update(f"{f.relative_to(src)}|{st.st_size}|{int(st.st_mtime)}".encode())
    root = Path(settings.USER_MODELS_BASE_DIR).parent / "sandbox_real"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    dest = root / sig.hexdigest()
    if dest.is_dir():
        return dest

    tmp = Path(tempfile.mkdtemp(prefix="tmp_", dir=root))
    try:
        shutil.copytree(src, tmp / "m", symlinks=False)
        for d in [tmp / "m", *(tmp / "m").rglob("*")]:
            os.chmod(d, 0o755 if d.is_dir() else 0o644)
        try:
            os.rename(tmp / "m", dest)
        except OSError:
            pass  # another process created it first
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest
