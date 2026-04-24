# ──────────────────────────────────────────────
# apps/users/model_lifecycle.py
#
# Model lifecycle helpers for the local-repo architecture.
#
# Post-refactor:  model files are git-committed under
#   user_models/user_{id}/{game}/model/
#   user_models/user_{id}/{game}/data/
# No HF downloads occur at runtime.
#
# Key public functions
# ────────────────────
#   ensure_user_dirs(user_id)       — create directory skeleton
#   verify_local_model_files(gm)    — check committed files exist
#   sync_hf_repo_to_local(…)        — compat stub (no-op / local verify)
#   download_hf_repo_files(…)       — compat stub (no-op)
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

try:
    from huggingface_hub import hf_hub_download, snapshot_download
except ImportError:  # pragma: no cover
    hf_hub_download = None  # type: ignore[assignment]
    snapshot_download = None  # type: ignore[assignment]


def _user_base_dir(user_id: int) -> Path:
    """Return per-user model cache base directory."""
    base = getattr(settings, "MODEL_CACHE_ROOT", None)
    if not base:
        base = getattr(settings, "USER_MODELS_BASE_DIR", Path("/tmp/user_models"))
    return Path(base) / f"user_{user_id}"


def _game_dest_dir(user_id: int, game_type: str) -> Path:
    """Return per-user per-game directory, e.g. {root}/user_{id}/{game_type}/."""
    return _user_base_dir(user_id) / game_type


def ensure_user_dirs(user_id: int) -> Path:
    """Ensure the per-user directory skeleton exists for both game types.

    Creates:
      {root}/user_{id}/chess/{model,data}/
      {root}/user_{id}/breakthrough/{model,data}/

    Returns the base user dir Path.
    """
    base = _user_base_dir(user_id)
    try:
        for game in ("chess", "breakthrough"):
            game_dir = base / game
            model_dir = game_dir / "model"
            data_dir = game_dir / "data"
            model_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)

            for d in (model_dir, data_dir):
                try:
                    (d / ".placeholder").write_text("Auto-created placeholder\n")
                except Exception:
                    try:
                        (d / "README.md").write_text(f"Auto-created for user {user_id} ({game})\n")
                    except Exception:
                        pass
    except Exception:
        log.exception("Failed to ensure user dirs for %s", user_id)
    return base


def _repo_folder_name(repo_id: str) -> str:
    """Sanitize a HF repo id to a filesystem-friendly folder name."""
    return (repo_id or "").replace("/", "__")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Local-repo file verification (post-refactor)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def verify_local_model_files(game_model) -> tuple[bool, str]:
    """Verify that committed model/data files exist for a UserGameModel.

    Delegates to ``apps.games.local_inference.verify_local_files``.
    Updates ``local_path`` and ``model_integrity_ok`` on success.

    Returns ``(ok, message)``.  Never raises.
    """
    try:
        from apps.games.local_inference import (
            verify_local_files,
            _game_type_dir,
        )

        ok, msg = verify_local_files(game_model)
        if ok:
            gt_dir = _game_type_dir(game_model.user_id, game_model.game_type)
            update_fields: list[str] = ["model_integrity_ok"]
            game_model.model_integrity_ok = True

            if hasattr(game_model, "local_path") and game_model.local_path != str(gt_dir):
                game_model.local_path = str(gt_dir)
                update_fields.append("local_path")

            if hasattr(game_model, "cached_path") and not game_model.cached_path:
                game_model.cached_path = str(gt_dir)
                update_fields.append("cached_path")

            try:
                game_model.save(update_fields=update_fields)
            except Exception:
                log.exception(
                    "verify_local_model_files: could not save game_model for user=%s game=%s",
                    game_model.user_id, game_model.game_type,
                )
        else:
            try:
                game_model.model_integrity_ok = False
                game_model.save(update_fields=["model_integrity_ok"])
            except Exception:
                pass

        return ok, msg
    except Exception as exc:
        log.exception(
            "verify_local_model_files: unexpected error for user=%s game=%s",
            getattr(game_model, "user_id", "?"),
            getattr(game_model, "game_type", "?"),
        )
        return False, str(exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Compatibility stubs (HF download replaced by local verify)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def download_hf_repo_files(user, game_model, *, token: str | None = None) -> None:
    """DEPRECATED — model files are now git-committed, not downloaded at runtime.

    This stub calls verify_local_model_files() to confirm committed files
    exist.  No network I/O is performed.
    """
    user_id = getattr(user, "pk", user)
    log.info(
        "download_hf_repo_files (stub): skipping HF download for user=%s game=%s — "
        "checking local committed files instead",
        user_id, getattr(game_model, "game_type", "?"),
    )
    verify_local_model_files(game_model)


def sync_hf_repo_to_local(user, game_model, *, token: str | None = None) -> tuple[bool, str]:
    """DEPRECATED — model files are now git-committed, not downloaded at runtime.

    This stub calls verify_local_model_files() and returns its result.
    No network I/O is performed.

    Returns ``(bool, message)`` for backwards compatibility.
    """
    user_id = getattr(user, "pk", user)
    log.info(
        "sync_hf_repo_to_local (stub): skipping HF snapshot for user=%s game=%s — "
        "checking local committed files instead",
        user_id, getattr(game_model, "game_type", "?"),
    )
    return verify_local_model_files(game_model)

def get_user_model_cache_dir(
    user,
    game_type: str,
    *,
    repo_id: str | None = None,
) -> Path:
    """Return the model cache dir, preferring the shared HF hub cache.

    If ``repo_id`` is supplied and a snapshot for that repo exists in
    ``settings.HF_HUB_CACHE``, that snapshot path is returned directly —
    no per-user copy is needed.  Falls back to the per-user game-type dir.

    Accepts either a ``CustomUser`` instance or a numeric user id.
    """
    uid = getattr(user, "pk", user)
    if repo_id:
        from apps.games.local_inference import _find_hf_cache_snapshot
        snap = _find_hf_cache_snapshot(repo_id)
        if snap is not None:
            return snap
    return _game_dest_dir(uid, game_type)


def _user_repo_cache_dir(user_id: int, repo_id: str, game_type: str) -> Path:
    """Return the final repo-specific cache directory for a given user/game.

    Layout: {root}/user_{user_id}/{game_type}/{owner__repo}/
    """
    base = _game_dest_dir(user_id, game_type)
    return base / _repo_folder_name(repo_id)


def _build_file_list(directory: Path) -> list[dict]:
    """Walk *directory* and return a list of {name, size} dicts.

    Paths are stored relative to *directory* so they are comparable
    across different download locations.
    """
    files = []
    if directory and directory.exists():
        for p in sorted(directory.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(directory))
                files.append({"name": rel, "size": p.stat().st_size})
    return files


def download_model_to_cache(
    game_model,
    *,
    token: str | None = None,
    force: bool = False,
) -> tuple[bool, str, Path | None]:
    """Download a HF model repository into the persistent per-user cache.

    This uses `huggingface_hub.snapshot_download()` and places the files
    under: {MODEL_CACHE_ROOT|USER_MODELS_BASE_DIR}/user_{id}/{game_type}/model/

    Returns (ok, message, model_dir).
    """
    if snapshot_download is None:
        return False, "huggingface_hub not available", None

    repo_id = (game_model.hf_model_repo_id or "").strip()
    if not repo_id:
        return False, "No repo_id provided", None

    dest = _game_dest_dir(game_model.user_id, game_model.game_type) / "model"

    # If already present and not forced, assume ready.
    if dest.exists() and not force:
        return True, "Already cached", dest

    # Ensure parent exists
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"Could not create parent dir: {exc}", None

    # If forcing, remove existing folder first
    if dest.exists() and force:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            log.debug("Could not remove existing cache dir %s", dest, exc_info=True)

    hf_token = token or _resolve_token(game_model.user)

    try:
        # Use a local HF cache inside the user's cache dir to avoid touching ~/.cache/huggingface
        cache_dir = dest / ".hf_cache"
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(dest),
            token=hf_token or None,
            allow_patterns=None,  # let verification step enforce allowed files
            cache_dir=str(cache_dir),
        )
    except Exception as exc:
        log.exception("snapshot_download failed for %s", repo_id)
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
        return False, f"Download failed: {exc}", None

    return True, "Downloaded to cache", dest


def _resolve_token(user) -> str:
    """Return the best HF token for *user*: their OAuth token if available,
    otherwise the platform token."""
    from apps.users.hf_oauth import get_user_hf_token
    user_token = get_user_hf_token(user)
    if user_token:
        return user_token
    return getattr(settings, "HF_PLATFORM_TOKEN", "")


def _diff_file_lists(
    old_list: list[dict],
    new_list: list[dict],
) -> list[str]:
    """Compare two file lists (name + size) and return changed file names."""
    old_map = {f["name"]: f["size"] for f in old_list}
    new_map = {f["name"]: f["size"] for f in new_list}

    changed: list[str] = []

    # Files added or changed in size
    for name, size in new_map.items():
        if name not in old_map:
            changed.append(name)
        elif old_map[name] != size:
            changed.append(name)

    # Files removed
    for name in old_map:
        if name not in new_map:
            changed.append(name)

    return sorted(changed)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task 1 — Login: download & scan every user model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@shared_task(bind=True, max_retries=0)
def download_and_scan_on_login(self, user_id: int):
    download_and_scan_for_user(user_id)


def download_and_scan_for_user(user_id: int) -> None:
    """No-op: models are served from the shared HF hub cache; no per-user copies.

    Previously this function downloaded HF repos into per-user directories.
    After the local-cache refactor, inference reads directly from
    ``settings.HF_HUB_CACHE`` snapshots resolved by
    ``local_inference._find_hf_cache_snapshot()``.  Calling this is safe
    but does nothing — the shared cache is populated via:

        huggingface-cli download <repo_id>
    """
    log.info(
        "download_and_scan_for_user: no-op for user=%s "
        "— models served from shared HF hub cache",
        user_id,
    )
    # Update cached_path to point at the HF cache snapshot for each game model.
    try:
        from apps.users.models import UserGameModel
        from apps.games.local_inference import _find_hf_cache_snapshot
        for gm in UserGameModel.objects.filter(user_id=user_id, hf_model_repo_id__gt=""):
            repo_id = (gm.hf_model_repo_id or "").strip()
            snap = _find_hf_cache_snapshot(repo_id) if repo_id else None
            if snap and (any(snap.rglob("*.safetensors")) or any(snap.rglob("*.py"))):
                if gm.cached_path != str(snap):
                    gm.cached_path = str(snap)
                    gm.model_integrity_ok = True
                    gm.save(update_fields=["cached_path", "model_integrity_ok"])
                    log.info(
                        "Pointed cached_path at HF cache for user=%s game=%s: %s",
                        user_id, gm.game_type, snap,
                    )
    except Exception:
        log.exception("download_and_scan_for_user: failed to update cached_path for user=%s", user_id)


# ─── dead code kept only for import-compatibility ───────────────────────────
_DEAD_CODE_PLACEHOLDER = None  # replaced old HF-download body



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task 2 — Logout: cleanup files & cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def cleanup_for_user(user_id: int) -> None:
    """Synchronous logout cleanup: delete model files and clear cache for *user_id*.

    Exposed so callers can run the cleanup synchronously without relying
    on a running Celery worker (mirrors ``download_and_scan_for_user``).
    """
    from apps.users.models import UserGameModel

    user_dir = _user_base_dir(user_id)
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
        log.info("Cleaned up model dir on logout: %s", user_dir)

    # Clear cache keys and cached_path for every game type this user has
    game_models = UserGameModel.objects.filter(user_id=user_id)
    for gm in game_models:
        cache.delete(f"model_status_{user_id}_{gm.game_type}")
        if gm.cached_path:
            gm.cached_path = ""
            gm.cached_at = None
            gm.cached_commit = ""
            gm.save(update_fields=["cached_path", "cached_at", "cached_commit"])

    log.info("Logout cleanup complete for user %s.", user_id)


@shared_task(bind=True, max_retries=0)
def cleanup_on_logout(self, user_id: int):
    """Celery task wrapper around :func:`cleanup_for_user`."""
    cleanup_for_user(user_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task 3 — Daily integrity check (all models)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@shared_task(bind=True, max_retries=0)
def daily_integrity_check(self):
    """Check every UserGameModel for commit SHA changes."""
    from apps.users.models import UserGameModel
    from apps.games.local_sandbox_inference import _get_current_commit_sha

    all_models = UserGameModel.objects.select_related("user").all()
    checked = 0
    flagged = 0

    for gm in all_models:
        repo_id = gm.hf_model_repo_id
        if not repo_id:
            continue

        token = _resolve_token(gm.user)
        current_sha = _get_current_commit_sha(repo_id, token)
        if not current_sha:
            log.warning("Could not fetch SHA for repo=%s (user=%s)", repo_id, gm.user_id)
            continue

        if current_sha == gm.last_verified_commit:
            log.info("No changes for user=%s game=%s repo=%s", gm.user_id, gm.game_type, repo_id)
            checked += 1
            continue

        # Commit changed — flag for re-verification
        log.warning(
            "Daily check: SHA changed for user=%s game=%s (old=%s new=%s)",
            gm.user_id, gm.game_type,
            (gm.last_verified_commit or "none")[:8], current_sha[:8],
        )
        gm.verification_status = "suspicious"
        gm.model_integrity_ok = False
        gm.save(update_fields=["verification_status", "model_integrity_ok"])
        flagged += 1
        checked += 1

    log.info("Daily integrity check complete: %d checked, %d flagged.", checked, flagged)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task 4 — Periodic cleanup of orphaned dirs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@shared_task(bind=True, max_retries=0)
def cleanup_orphaned_dirs(self):
    """Remove /tmp/user_models/{user_id}/ dirs for users with no active session."""
    from django.contrib.sessions.models import Session

    base = getattr(settings, "USER_MODELS_BASE_DIR", Path("/tmp/user_models"))
    if not base.exists():
        return

    # Collect user IDs from all non-expired sessions
    active_user_ids: set[str] = set()
    now = timezone.now()
    for session in Session.objects.filter(expire_date__gt=now):
        try:
            data = session.get_decoded()
            uid = data.get("_auth_user_id")
            if uid:
                active_user_ids.add(str(uid))
        except Exception:
            continue

    removed = 0
    for entry in base.iterdir():
        if entry.is_dir() and entry.name not in active_user_ids:
            shutil.rmtree(entry, ignore_errors=True)
            log.info("Removed orphaned model dir: %s", entry)
            removed += 1

    log.info("Orphaned dir cleanup: removed %d dirs (%d active sessions).",
             removed, len(active_user_ids))
