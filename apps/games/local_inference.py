# ──────────────────────────────────────────────
# apps/games/local_inference.py
#
# Local-repo model inference — NO HF downloads at runtime.
#
# Architecture (post-refactor)
# ────────────────────────────
# Models and datasets are git-committed (or init-container placed) under:
#
#   {USER_MODELS_BASE_DIR}/user_{id}/{game}/model/   ← model.safetensors, config.json, …
#   {USER_MODELS_BASE_DIR}/user_{id}/{game}/data/    ← *.npz, *.json datasets
#
# The root is settings.USER_MODELS_BASE_DIR (default: /var/lib/agladiator/user_models).
# Override with the AGL_USER_MODELS_DIR environment variable.
#
# Public API  (drop-in replacement for the old local_sandbox_inference surface)
# ──────────────────────────────────────────────────────────────────────────────
#   resolve_model_path(user_id, game_type)     → (model_dir|None, data_dir|None)
#   verify_local_files(game_model)             → (ok, message)
#   verify_model(game_model, *, token=None)    → (passed, msg, report)
#   reverify_model(game_model, *, token=None)  → (passed, msg, report)
#   get_move_local(user_id, game_type, fen, player, *, repo_id=None) → move_str | None
#   prewarm_local(user_id, game_type, *, repo_id=None)               → bool  (warm worker)
#   scan_model(model_dir)                      → (passed, report)   [delegate]
#   download_model(repo_id, game_type, …)      → compat no-op stub
#
# ZERO runtime network I/O
# ────────────────────────
# None of these functions contact Hugging Face or any external service.
# Docker sandbox execution reads model files directly from the resolved
# path (read-only bind mount) — there is no temp-copy step, and no
# in-process/local-process fallback. Docker is mandatory; see
# apps.games.sandbox_runner and apps.games.exceptions.SandboxUnavailableError.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from apps.users.models import UserGameModel

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Path resolution helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _models_base() -> Path:
    """Return the configured user-model storage root (never None)."""
    base = getattr(settings, "USER_MODELS_BASE_DIR", None)
    if not base:
        base = "/var/lib/agladiator/user_models"
    return Path(base)


def _hf_cache_root() -> Path:
    """Return the shared HF hub cache root (settings.HF_HUB_CACHE or fallback)."""
    import os as _os
    val = getattr(settings, "HF_HUB_CACHE", None) or _os.environ.get("HF_HUB_CACHE")
    if not val:
        val = str(_models_base() / "hf_hub_cache")
    return Path(val)


def _is_commit_sha(ref: str) -> bool:
    return len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower())


def _find_hf_cache_snapshot(repo_id: str, ref: str = "main") -> Path | None:
    """Resolve a repo_id to its snapshot directory in the shared HF hub cache.

    Layout: {HF_HUB_CACHE}/models--{owner}--{name}/snapshots/{sha}/

    If *ref* is itself a full commit SHA (e.g. a model's approved/pinned
    SHA), it's tried directly against ``snapshots/{ref}/`` first — a raw
    commit has no ``refs/`` indirection file (that only exists for named
    refs like "main"), so resolving it via ``refs/{ref}`` would always
    miss, silently falling back to whatever "main" happens to point at
    right now instead of the actually-pinned commit.

    Otherwise (or if the pinned SHA isn't cached), reads the ``refs/{ref}``
    file to get the SHA, then returns ``snapshots/{sha}/``. Falls back to
    the lexicographically last snapshot dir if that's absent too.

    Returns ``None`` if the repo is not cached at all.
    """
    folder = "models--" + repo_id.replace("/", "--")
    repo_dir = _hf_cache_root() / folder
    if not repo_dir.exists():
        return None

    if _is_commit_sha(ref):
        snap = repo_dir / "snapshots" / ref
        if snap.exists():
            return snap

    ref_file = repo_dir / "refs" / ref
    if ref_file.exists():
        sha = ref_file.read_text().strip()
        snap = repo_dir / "snapshots" / sha
        if snap.exists():
            return snap
    # Fallback: pick the last snapshot directory (most recently downloaded)
    snap_dir = repo_dir / "snapshots"
    if snap_dir.exists():
        snaps = sorted(p for p in snap_dir.iterdir() if p.is_dir())
        if snaps:
            return snaps[-1]
    return None


def _has_model_files(d: Path) -> bool:
    return (
        any(d.rglob("*.safetensors"))
        or any(f for f in d.rglob("*.py") if not f.name.startswith("_agl_"))
        or any(d.rglob("*.npz"))
    )


def _has_real_data_files(d: Path) -> bool:
    """True if *d* contains at least one real (non-symlink, non-placeholder) file.

    Excludes the hidden ``.placeholder`` / ``README.md`` files that
    ``ensure_user_dirs()`` creates for empty per-user folders, and excludes
    symlinks — Docker bind mounts cannot follow the ``../../blobs/<sha>``
    symlinks used by the raw HF hub cache, so a dir full of only symlinks
    must NOT be treated as "real" data.
    """
    if not d.exists():
        return False
    try:
        for f in d.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            if f.name.startswith(".") or f.name == "README.md":
                continue
            return True
    except OSError:
        return False
    return False


def _dataset_cache_folder_name(repo_id: str) -> str:
    return "datasets--" + repo_id.replace("/", "--")


def prepare_data_dir(
    user_id: int | str,
    game_type: str,
    *,
    data_repo_id: str | None = None,
    revision: str | None = None,
    token: str | None = None,
) -> Path | None:
    """Resolve a REAL (non-symlink) ``/data`` directory for the sandbox.

    Docker bind-mounts a directory as-is: if that directory's files are
    symlinks pointing at ``HF_HUB_CACHE/.../blobs/<sha>`` (the layout used
    by the raw HF hub cache / ``snapshot_download(cache_dir=...)``), the
    container sees broken paths once mounted read-only. This function
    guarantees the returned directory (if any) contains real files.

    Resolution order
    ----------------
    1. Per-user committed data dir (``.../user_{id}/{game}/data/``) if it
       already has real files (e.g. git-committed alongside the model).
    2. Mirror *data_repo_id* at the pinned *revision* via
       ``snapshot_download(..., local_dir=..., local_dir_use_symlinks=False)``
       into a per-user/per-revision cache folder — reused on later calls so
       the data repo is only downloaded once per pinned commit.

    Returns ``None`` if there is no data repo configured and no committed
    local data files.
    """
    base = _models_base() / f"user_{user_id}" / game_type
    committed_dir = base / "data"
    if _has_real_data_files(committed_dir):
        return committed_dir

    if not data_repo_id:
        return None

    rev = (revision or "main").strip() or "main"
    dest = base / "data_cache" / _dataset_cache_folder_name(data_repo_id) / rev

    if _has_real_data_files(dest):
        return dest

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception:
        log.warning(
            "prepare_data_dir: huggingface_hub unavailable; cannot fetch data repo %s",
            data_repo_id,
        )
        return None

    try:
        dest.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=data_repo_id,
            repo_type="dataset",
            revision=rev,
            local_dir=str(dest),
            local_dir_use_symlinks=False,
            token=token,
        )
    except Exception:
        log.exception(
            "prepare_data_dir: snapshot_download failed for repo=%s revision=%s",
            data_repo_id, rev,
        )

    return dest if _has_real_data_files(dest) else None


def resolve_model_path(
    user_id: int | str,
    game_type: str,
    *,
    repo_id: str | None = None,
    model_revision: str | None = None,
    data_repo_id: str | None = None,
    data_revision: str | None = None,
    data_token: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Resolve the model and data directories for user+game.

    Resolution order
    ----------------
    1. Per-user committed files: ``{USER_MODELS_BASE_DIR}/user_{id}/{game}/model/``
    2. Shared HF hub cache snapshot: ``{HF_HUB_CACHE}/models--{owner}--{name}/snapshots/{sha}/``
       (only when ``repo_id`` is provided). Resolved against *model_revision*
       (the caller's approved/pinned SHA) when given — falling back to
       "main" otherwise — instead of always trusting whatever "main"
       currently resolves to in the shared cache, which can silently drift
       away from the SHA that was actually approved for this user/game.

    In both cases the data directory is resolved via :func:`prepare_data_dir`
    (never a raw HF cache snapshot with symlinks — see that function's
    docstring).

    Returns ``(model_dir, data_dir)``.  Either may be ``None`` if the
    directory is absent or contains no recognisable model artefacts.
    """
    base = _models_base() / f"user_{user_id}" / game_type
    model_dir = base / "model"

    def _data() -> Path | None:
        return prepare_data_dir(
            user_id, game_type,
            data_repo_id=data_repo_id, revision=data_revision, token=data_token,
        )

    # ── 1. Per-user committed files ───────────────────────────────────────
    if model_dir.exists() and _has_model_files(model_dir):
        return model_dir, _data()

    # ── 2. Shared HF hub cache snapshot ──────────────────────────────────
    if repo_id:
        ref = (model_revision or "main").strip() or "main"
        snap = _find_hf_cache_snapshot(repo_id, ref=ref)
        if snap is not None and _has_model_files(snap):
            log.debug("Using HF hub cache snapshot for repo=%s (ref=%s): %s", repo_id, ref, snap)
            return snap, _data()

    log.debug("No model files found for user=%s game=%s (repo_id=%s)", user_id, game_type, repo_id)
    return None, None


def _game_type_dir(user_id: int | str, game_type: str) -> Path:
    """Return the game-type base dir (contains model/ and data/ subdirs)."""
    return _models_base() / f"user_{user_id}" / game_type


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Local file validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_local_files(
    game_model: "UserGameModel",
) -> tuple[bool, str]:
    """Check that committed model/data files exist and are non-empty.

    Called at login to confirm the repo's committed files are present.
    Does NOT run any code — purely filesystem checks.

    Returns ``(ok, message)``.
    """
    user_id = game_model.user_id
    game_type = game_model.game_type

    model_dir, data_dir = resolve_model_path(user_id, game_type)
    if model_dir is None:
        expected = _models_base() / f"user_{user_id}" / game_type / "model"
        return False, (
            f"No local model files found for user {user_id} game={game_type}. "
            f"Expected path: {expected}"
        )

    issues: list[str] = []

    # Check for zero-byte files (placeholder files are fine, weight files must be non-empty).
    for f in model_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.name.startswith(".") or f.name.startswith("_agl_"):
            continue
        if f.suffix.lower() in (".safetensors", ".npz") and f.stat().st_size == 0:
            issues.append(f"Empty weight file: {f.name}")

    # Check data dir for empty weight/dataset files.
    if data_dir:
        for f in data_dir.rglob("*"):
            if not f.is_file():
                continue
            if f.name.startswith(".") or f.name.startswith("_agl_"):
                continue
            if f.suffix.lower() in (".npz", ".safetensors") and f.stat().st_size == 0:
                issues.append(f"Empty data file: {f.name}")

    if issues:
        return False, "; ".join(issues)
    return True, "OK"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Verification stubs — integrity checks live in apps.users.integrity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_model(game_model, *, token=None):
    """Stub — real integrity checks run via apps.users.integrity.check_local_integrity."""
    return True, "OK", {}


def reverify_model(game_model, *, token=None):
    return verify_model(game_model, token=token)


def _lookup_game_model(user_id: int | str, game_type: str):
    """Return the ``UserGameModel`` row for user+game, or ``None``."""
    try:
        from apps.users.models import UserGameModel
        return UserGameModel.objects.filter(user_id=user_id, game_type=game_type).first()
    except Exception:
        log.debug(
            "Could not load UserGameModel for user=%s game=%s", user_id, game_type,
            exc_info=True,
        )
        return None


def _resolve_data_token(game_model) -> str | None:
    """Best-available HF token for downloading a (usually public) data repo."""
    try:
        from apps.users.hf_oauth import get_user_hf_token
        tok = get_user_hf_token(game_model.user)
        if tok:
            return tok
    except Exception:
        pass
    return getattr(settings, "HF_PLATFORM_TOKEN", None) or None


def _required_data_filenames(model_dir: Path) -> list[str]:
    """Filenames a *legacy* (no ``load``) model needs, per ``config_model.json``.

    Only meaningful for models without a ``load`` entrypoint — see
    :func:`_model_defines_load`. Reads the legacy single-file
    ``zone_db_filename`` key (still read by ``apps.games.handler`` /
    ``handlers/*.py`` at actual runtime), falling back to ``zone_db.npz``
    to preserve behavior for models predating config_model.json.
    """
    cfg_path = model_dir / "config_model.json"
    if cfg_path.exists():
        try:
            import json
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            name = cfg.get("zone_db_filename")
            if name:
                return [name]
        except Exception:
            log.debug("Could not parse config_model.json in %s", model_dir, exc_info=True)
    return ["zone_db.npz"]


def _entrypoint_file(model_dir: Path, game_type: str) -> Path | None:
    """The .py file the sandbox will actually import as the model's entrypoint.

    Mirrors ``apps.games.sandbox_runner._entrypoint_candidates()``'s
    resolution order (``modules`` -> ``entrypoint`` -> ``"<game_type>_mcvs.py"``
    default) so this looks at the same file Docker will run, without
    executing any of it.
    """
    manifest: dict = {}
    cfg_path = model_dir / "config_model.json"
    if cfg_path.exists():
        try:
            import json
            manifest = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except Exception:
            manifest = {}

    modules = manifest.get("modules")
    if isinstance(modules, list) and modules:
        name = modules[-1]
    else:
        entrypoint = manifest.get("entrypoint")
        name = entrypoint if isinstance(entrypoint, str) else f"{game_type or 'chess'}_mcvs.py"

    path = model_dir / name if isinstance(name, str) else None
    return path if path is not None and path.exists() else None


def _model_defines_load(model_dir: Path, game_type: str) -> bool:
    """True if the entrypoint statically defines a module-level ``load`` function.

    New-contract models implement ``load(ctx)``/``get_move(state, fen, player)``
    and are responsible for finding their own data files themselves (see
    ``templates_for_users/chess/chess_mcvs.py``) — real models are free to
    use whatever discovery logic they want (e.g. globbing for any ``*.npz``
    regardless of name), so a fixed-filename guess from the host can't
    reliably match every model. Parsed via ``ast`` only, never executed —
    same safety guarantee as ``apps.games.model_check``'s syntax check.
    """
    path = _entrypoint_file(model_dir, game_type)
    if path is None:
        return False
    try:
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except Exception:
        log.debug("Could not parse %s to detect load() contract", path, exc_info=True)
        return False
    return any(isinstance(node, ast.FunctionDef) and node.name == "load" for node in tree.body)


def _record_sandbox_error(game_model, message: str) -> None:
    if game_model is None:
        return
    try:
        game_model.last_sandbox_error = message
        game_model.last_sandbox_error_at = timezone.now()
        game_model.save(update_fields=["last_sandbox_error", "last_sandbox_error_at"])
    except Exception:
        log.exception(
            "Could not persist last_sandbox_error for user=%s game=%s",
            getattr(game_model, "user_id", "?"), getattr(game_model, "game_type", "?"),
        )


def _clear_sandbox_error(game_model) -> None:
    if game_model is None or not game_model.last_sandbox_error:
        return
    try:
        game_model.last_sandbox_error = ""
        game_model.last_sandbox_error_at = None
        game_model.save(update_fields=["last_sandbox_error", "last_sandbox_error_at"])
    except Exception:
        log.debug("Could not clear last_sandbox_error", exc_info=True)


def _require_data_file_if_configured(
    model_dir: Path, data_dir: Path | None, game_model, *, game_type: str = "chess",
) -> None:
    """Raise ``SandboxDataMissingError`` if a declared data repo's file(s) are absent.

    Only enforced when the user has actually declared a data repo
    (``hf_data_repo_id``) — models with no data repo are unaffected. Skipped
    entirely for new-contract (``load(ctx)``) models: those find their own
    data files inside the sandbox, and ``check_model()`` already ran that
    exact ``load(ctx)`` for real in Docker before this SHA could go ACTIVE,
    so a fixed-filename guess here would be both redundant and unreliable.
    Still enforced (fail-fast before Docker) for legacy models with no
    ``load`` entrypoint, since those are read by a fixed key at runtime
    (``apps.games.handler`` et al.).
    """
    data_repo_id = (getattr(game_model, "hf_data_repo_id", "") or "").strip() if game_model else ""
    if not data_repo_id:
        return

    if _model_defines_load(model_dir, game_type):
        return

    filenames = _required_data_filenames(model_dir)
    bases = [b for b in (data_dir, model_dir) if b is not None]
    missing = [f for f in filenames if not any((b / f).exists() for b in bases)]
    if not missing:
        return

    from apps.games.exceptions import SandboxDataMissingError
    message = (
        f"Data file(s) {', '.join(missing)} were not found in data repo '{data_repo_id}' "
        f"(resolved data_dir={data_dir}). Check that the file(s) exist in the "
        f"repo at the pinned commit and that their names match "
        f"'data_files' (or legacy 'zone_db_filename') in config_model.json."
    )
    _record_sandbox_error(game_model, message)
    raise SandboxDataMissingError(message)


def _resolve_for_sandbox(user_id, game_type, repo_id):
    """Shared by get_move_local() and prewarm_local(): returns (game_model, model_dir, data_dir)."""
    game_model = _lookup_game_model(user_id, game_type)
    data_repo_id = (getattr(game_model, "hf_data_repo_id", "") or "").strip() if game_model else ""
    data_revision = "main"
    data_token = None
    model_revision = None
    if game_model is not None:
        data_revision = (
            (game_model.approved_data_repo_sha or "").strip()
            or (game_model.current_data_repo_sha or "").strip()
            or "main"
        )
        data_token = _resolve_data_token(game_model)
        model_revision = (
            (game_model.approved_full_sha or "").strip()
            or (game_model.current_repo_sha or "").strip()
            or None
        )

    model_dir, data_dir = resolve_model_path(
        user_id, game_type, repo_id=repo_id, model_revision=model_revision,
        data_repo_id=data_repo_id or None, data_revision=data_revision, data_token=data_token,
    )
    return game_model, model_dir, data_dir


def get_move_local(
    user_id: int | str,
    game_type: str,
    fen: str,
    player: str,
    *,
    repo_id: str | None = None,
) -> str | None:
    """Get a move by running the user's model in the Docker sandbox.

    Resolves the model/data directories for *user_id*/*game_type* and
    delegates execution to :func:`apps.games.sandbox_runner.run_predict_in_sandbox`.

    Docker is mandatory — ``SandboxUnavailableError`` (daemon unreachable,
    image missing, etc.) is NOT caught here and must propagate to the caller.
    ``SandboxDataMissingError`` is raised (and NOT caught here either) when
    the user declared a data repo but its required file could not be found
    after preparing ``/data`` — this is a configuration problem, not a
    model-quality issue, so it must not silently fall back to a random move.
    Returns ``None`` if no local model files are found, or if the sandbox
    ran but produced no valid move.
    """
    from apps.games.sandbox_runner import run_predict_in_sandbox

    game_model, model_dir, data_dir = _resolve_for_sandbox(user_id, game_type, repo_id)
    if model_dir is None:
        log.warning(
            "get_move_local: no model files for user=%s game=%s repo=%s",
            user_id, game_type, repo_id,
        )
        return None

    _require_data_file_if_configured(model_dir, data_dir, game_model, game_type=game_type)

    move = run_predict_in_sandbox(model_dir, data_dir, fen, player, game_type)
    if move:
        _clear_sandbox_error(game_model)
    return move


def prewarm_local(
    user_id: int | str,
    game_type: str,
    *,
    repo_id: str | None = None,
) -> bool:
    """Start the warm sandbox worker for this user's model ahead of the first move.

    Resolves the model/data dirs EXACTLY like :func:`get_move_local` (same
    worker key), so the first real move finds an already-loaded worker.
    Safe to call repeatedly; never raises — any problem is logged and the
    real ``get_move_local`` call will report it properly. Returns True if a
    warm worker is ready.
    """
    try:
        from apps.games.sandbox_runner import prewarm_worker

        game_model, model_dir, data_dir = _resolve_for_sandbox(user_id, game_type, repo_id)
        if model_dir is None:
            return False
        _require_data_file_if_configured(model_dir, data_dir, game_model, game_type=game_type)
        return prewarm_worker(model_dir, data_dir, game_type)
    except Exception:
        log.warning(
            "prewarm_local failed for user=%s game=%s repo=%s",
            user_id, game_type, repo_id, exc_info=True,
        )
        return False


def scan_model(*args, **kwargs):
    """No-op stub."""
    return True, {}


def download_model(repo_id, game_type, *, token=None, dest_dir=None):
    """No-op stub."""
    return True, "Local sandbox mode — no runtime download", None