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
#   get_move_local(repo_id, fen, player, …)    → move_str | None
#   scan_model(model_dir)                      → (passed, report)   [delegate]
#   download_model(repo_id, game_type, …)      → compat no-op stub
#
# ZERO runtime network I/O
# ────────────────────────
# None of these functions contact Hugging Face or any external service.
# Docker sandbox execution (or local-process fallback) reads model files
# directly from the repo path — there is no temp-copy step.
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


def _find_hf_cache_snapshot(repo_id: str, ref: str = "main") -> Path | None:
    """Resolve a repo_id to its snapshot directory in the shared HF hub cache.

    Layout: {HF_HUB_CACHE}/models--{owner}--{name}/snapshots/{sha}/

    Reads the ``refs/{ref}`` file to get the pinned SHA, then returns
    ``snapshots/{sha}/``.  Falls back to the lexicographically last
    snapshot dir if the ref file is absent.

    Returns ``None`` if the repo is not cached at all.
    """
    folder = "models--" + repo_id.replace("/", "--")
    repo_dir = _hf_cache_root() / folder
    if not repo_dir.exists():
        return None
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


def resolve_model_path(
    user_id: int | str,
    game_type: str,
    *,
    repo_id: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Resolve the model and data directories for user+game.

    Resolution order
    ----------------
    1. Per-user committed files: ``{USER_MODELS_BASE_DIR}/user_{id}/{game}/model/``
    2. Shared HF hub cache snapshot: ``{HF_HUB_CACHE}/models--{owner}--{name}/snapshots/{sha}/``
       (only when ``repo_id`` is provided)

    Returns ``(model_dir, data_dir)``.  Either may be ``None`` if the
    directory is absent or contains no recognisable model artefacts.
    """
    base = _models_base() / f"user_{user_id}" / game_type
    model_dir = base / "model"
    data_dir = base / "data"

    def _has_model(d: Path) -> bool:
        return (
            any(d.rglob("*.safetensors"))
            or any(f for f in d.rglob("*.py") if not f.name.startswith("_agl_"))
            or any(d.rglob("*.npz"))
        )

    def _resolve_data(dd: Path) -> Path | None:
        if not dd.exists():
            return None
        try:
            non_hidden = [f for f in dd.iterdir() if not f.name.startswith(".")]
            return dd if non_hidden else None
        except OSError:
            return None

    # ── 1. Per-user committed files ───────────────────────────────────────
    if model_dir.exists() and _has_model(model_dir):
        return model_dir, _resolve_data(data_dir)

    # ── 2. Shared HF hub cache snapshot ──────────────────────────────────
    if repo_id:
        snap = _find_hf_cache_snapshot(repo_id)
        if snap is not None and _has_model(snap):
            log.debug("Using HF hub cache snapshot for repo=%s: %s", repo_id, snap)
            # Per-user data dir still used for datasets if present
            return snap, _resolve_data(data_dir)

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
#  Stubs — sandbox removed; inference via HF API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_model(game_model, *, token=None):
    """Stub — delegates to HF API integrity check."""
    from apps.games.hf_inference import verify_model as _vm
    return _vm(game_model, token=token)


def reverify_model(game_model, *, token=None):
    return verify_model(game_model, token=token)


def get_move_local(*args, **kwargs):
    """Removed — use apps.games.hf_inference.get_move_api()."""
    log.warning("get_move_local() called on stub — sandbox removed")
    return None


def scan_model(*args, **kwargs):
    """No-op stub."""
    return True, {}


def download_model(repo_id, game_type, *, token=None, dest_dir=None):
    """No-op stub."""
    return True, "HF API mode — no local download", None

