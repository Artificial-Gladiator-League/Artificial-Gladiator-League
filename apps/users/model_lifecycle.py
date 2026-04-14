# ──────────────────────────────────────────────
# apps/users/model_lifecycle.py
#
# Celery tasks for the full model lifecycle:
#   1. download_and_scan_on_login  — pull & scan on user login
#   2. cleanup_on_logout           — delete files on logout
#   3. daily_integrity_check       — scheduled daily at 12:00
#   4. cleanup_orphaned_dirs       — every 30 min, prune stale dirs
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


def _user_base_dir(user_id: int) -> Path:
    """Return per-user model cache base directory.

    Preferred root is `settings.MODEL_CACHE_ROOT` (new). Falls back to
    `settings.USER_MODELS_BASE_DIR` for backwards compatibility.
    Directory layout: {root}/user_{user_id}/
    """
    base = getattr(settings, "MODEL_CACHE_ROOT", None)
    if not base:
        base = getattr(settings, "USER_MODELS_BASE_DIR", Path("/tmp/user_models"))
    # Ensure a stable folder name so it's easy to reason about on-disk
    return Path(base) / f"user_{user_id}"


def _game_dest_dir(user_id: int, game_type: str) -> Path:
    """Return per-user per-game directory, e.g. {root}/user_{id}/{game_type}/."""
    return _user_base_dir(user_id) / game_type


def _repo_folder_name(repo_id: str) -> str:
    """Sanitize a HF repo id to a filesystem-friendly folder name.

    Example: 'austindavis/MyModel' -> 'austindavis__MyModel'
    """
    return (repo_id or "").replace("/", "__")


def get_user_model_cache_dir(user, game_type: str) -> Path:
    """Public helper: return the per-user cache directory for a game type.

    Accepts either a `CustomUser` instance or a numeric user id.
    """
    uid = getattr(user, "pk", user)
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
    under: {MODEL_CACHE_ROOT|USER_MODELS_BASE_DIR}/user_{id}/{game_type}/{owner__repo}/

    Returns (ok, message, repo_path).
    """
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return False, "huggingface_hub not available", None

    repo_id = (game_model.hf_model_repo_id or "").strip()
    if not repo_id:
        return False, "No repo_id provided", None

    dest = _user_repo_cache_dir(game_model.user_id, repo_id, game_model.game_type)

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
    """Download all UserGameModel repos for *user_id*, scan each."""
    from apps.users.models import CustomUser, UserGameModel
    from apps.games.local_sandbox_inference import (
        download_model,
        _download_data_repo,
        scan_model,
        _get_current_commit_sha,
        _cleanup_temp_dir,
    )
    game_models = list(UserGameModel.objects.select_related("user").filter(user_id=user_id))
    if not game_models:
        log.info("User %s has no UserGameModel entries — skipping login scan.", user_id)
        return

    user = game_models[0].user
    token = _resolve_token(user)

    for gm in game_models:
        game_type = gm.game_type
        repo_id = (gm.hf_model_repo_id or "").strip()
        if not repo_id:
            log.debug("No repo configured for user=%s game=%s — skipping", user_id, game_type)
            continue

        # Efficient skip: if we already have a verified cached snapshot and
        # the commit matches the last verified commit (or the cache is recent),
        # skip re-downloading.
        try:
            skip = False
            if gm.model_integrity_ok and gm.cached_path:
                p = Path(gm.cached_path)
                model_dir = p / "model"
                if model_dir.exists():
                    if gm.last_verified_commit and gm.cached_commit and gm.cached_commit == gm.last_verified_commit:
                        skip = True
                    elif gm.cached_at:
                        # Consider the cache fresh for up to 7 days to avoid
                        # repeated HF requests on frequent logins.
                        from django.utils import timezone as _tz
                        if (_tz.now() - gm.cached_at).days < 7:
                            skip = True
            if skip:
                cache_key = f"model_status_{user_id}_{game_type}"
                cache.set(cache_key, "model_ready", timeout=None)
                log.info("Skipping download — cached model present for user=%s game=%s", user_id, game_type)
                continue
        except Exception:
            log.debug("Error checking cache status for user=%s game=%s", user_id, game_type, exc_info=True)

        # Download into the per-repo persistent cache and verify in-place.
        repo_cache_dir = _user_repo_cache_dir(user_id, repo_id, game_type)

        ok, msg, repo_path = download_model_to_cache(gm, token=token, force=False)
        if not ok or repo_path is None:
            log.warning(
                "Cache download failed for user=%s game=%s repo=%s: %s",
                user_id, game_type, gm.hf_model_repo_id, msg,
            )
            gm.model_integrity_ok = False
            gm.save(update_fields=["model_integrity_ok"])
            # Best-effort cleanup
            try:
                if repo_cache_dir.exists():
                    shutil.rmtree(repo_cache_dir, ignore_errors=True)
            except Exception:
                log.debug("Failed to remove repo_cache_dir after failed download: %s", repo_cache_dir, exc_info=True)
            continue

        # ── Download data repo (if any) into repo_path/data ──
        data_dir = None
        if gm.hf_data_repo_id:
            d_ok, d_msg, d_path = _download_data_repo(
                gm.hf_data_repo_id,
                repo_path,
                token=token,
            )
            if not d_ok:
                log.warning(
                    "Data download failed for user=%s game=%s: %s",
                    user_id, game_type, d_msg,
                )
            else:
                data_dir = d_path

        # ── Scan model ──
        scan_passed, report = scan_model(repo_path)

        # Build current file list for diffing
        current_file_list = _build_file_list(repo_path)
        report["file_list"] = current_file_list

        # ── Compare commit SHA ──
        current_sha = _get_current_commit_sha(gm.hf_model_repo_id, token)

        if not scan_passed:
            # Scan failed — block and delete the repo cache
            log.warning(
                "Scan FAILED for user=%s game=%s repo=%s",
                user_id, game_type, gm.hf_model_repo_id,
            )
            gm.model_integrity_ok = False
            gm.scan_report = report
            gm.save(update_fields=["model_integrity_ok", "scan_report"])
            try:
                shutil.rmtree(repo_path, ignore_errors=True)
            except Exception:
                log.debug("Failed to remove repo_path after failed scan: %s", repo_path, exc_info=True)
            continue

        if gm.last_verified_commit and current_sha and current_sha != gm.last_verified_commit:
            # Commit changed since last verification
            old_file_list = gm.scan_report.get("file_list", []) if gm.scan_report else []
            changed_files = _diff_file_lists(old_file_list, current_file_list)
            report["changed_files"] = changed_files

            log.warning(
                "Model CHANGED for user=%s game=%s: %d files differ (old SHA=%s new SHA=%s)",
                user_id, game_type, len(changed_files),
                gm.last_verified_commit[:8], current_sha[:8] if current_sha else "?",
            )

            gm.verification_status = "suspicious"
            gm.model_integrity_ok = False
            gm.scan_report = report
            gm.rated_games_since_revalidation = 0
            gm.save(update_fields=[
                "verification_status",
                "model_integrity_ok",
                "scan_report",
                "rated_games_since_revalidation",
            ])
            # Keep files for re-verification — do NOT delete
        else:
            # Same commit or first-time verification — scan passed
            gm.model_integrity_ok = True
            gm.scan_report = report
            if current_sha:
                gm.last_verified_commit = current_sha
            gm.last_verified_at = timezone.now()
            gm.last_model_validation_date = timezone.now().date()
            # Persist the cached path so _find_cached_model() can locate these files during live-game inference.
            gm.cached_path = str(repo_path)
            gm.cached_at = timezone.now()
            gm.cached_commit = current_sha or ""
            gm.save(update_fields=[
                "model_integrity_ok",
                "scan_report",
                "last_verified_commit",
                "last_verified_at",
                "last_model_validation_date",
                "cached_path",
                "cached_at",
                "cached_commit",
            ])

            cache_key = f"model_status_{user_id}_{game_type}"
            cache.set(cache_key, "model_ready", timeout=None)
            log.info(
                "Model OK for user=%s game=%s (SHA=%s)",
                user_id, game_type, current_sha[:8] if current_sha else "n/a",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task 2 — Logout: cleanup files & cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@shared_task(bind=True, max_retries=0)
def cleanup_on_logout(self, user_id: int):
    """Delete model files and clear cache for *user_id*."""
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task 3 — Daily integrity check (all models)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@shared_task(bind=True, max_retries=0)
def daily_integrity_check(self):
    """Check every UserGameModel for commit SHA changes."""
    from apps.users.models import UserGameModel
    from apps.games.local_sandbox_inference import (
        download_model,
        scan_model,
        _get_current_commit_sha,
        _cleanup_temp_dir,
    )

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

        # Commit changed — download to temp dir, scan and diff
        log.info(
            "Daily check: SHA changed for user=%s game=%s (old=%s new=%s)",
            gm.user_id, gm.game_type,
            (gm.last_verified_commit or "none")[:8], current_sha[:8],
        )

        import tempfile
        temp_base = Path(tempfile.mkdtemp(prefix="aglad_daily_"))
        try:
            ok, msg, model_dir = download_model(
                repo_id,
                gm.game_type,
                token=token,
                dest_dir=temp_base,
            )
            if not ok:
                log.warning("Daily download failed for %s: %s", repo_id, msg)
                gm.model_integrity_ok = False
                gm.save(update_fields=["model_integrity_ok"])
                continue

            scan_passed, report = scan_model(model_dir)

            # Build file list and diff against stored list
            current_file_list = _build_file_list(model_dir)
            report["file_list"] = current_file_list

            old_file_list = gm.scan_report.get("file_list", []) if gm.scan_report else []
            changed_files = _diff_file_lists(old_file_list, current_file_list)
            report["changed_files"] = changed_files

            gm.verification_status = "suspicious"
            gm.model_integrity_ok = False
            gm.scan_report = report
            gm.rated_games_since_revalidation = 0
            gm.save(update_fields=[
                "verification_status",
                "model_integrity_ok",
                "scan_report",
                "rated_games_since_revalidation",
            ])
            flagged += 1
            log.warning(
                "Daily check FLAGGED user=%s game=%s: %d files changed",
                gm.user_id, gm.game_type, len(changed_files),
            )
        finally:
            # Always clean up temp files after daily scan
            _cleanup_temp_dir(temp_base)

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
