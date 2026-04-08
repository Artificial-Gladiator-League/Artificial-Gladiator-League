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
    """Return /tmp/user_models/{user_id}/."""
    base = getattr(settings, "USER_MODELS_BASE_DIR", Path("/tmp/user_models"))
    return base / str(user_id)


def _game_dest_dir(user_id: int, game_type: str) -> Path:
    """Return /tmp/user_models/{user_id}/{game_type}/."""
    return _user_base_dir(user_id) / game_type


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

    # If user dir already exists, wipe it and re-download fresh
    user_dir = _user_base_dir(user_id)
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)
        log.info("Removed stale user dir %s before fresh download.", user_dir)

    for gm in game_models:
        game_type = gm.game_type
        dest_dir = _game_dest_dir(user_id, game_type)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # ── Download model repo ──
        ok, msg, model_dir = download_model(
            gm.hf_model_repo_id,
            game_type,
            token=token,
            dest_dir=dest_dir,
        )
        if not ok:
            log.warning(
                "Download failed for user=%s game=%s repo=%s: %s",
                user_id, game_type, gm.hf_model_repo_id, msg,
            )
            gm.model_integrity_ok = False
            gm.save(update_fields=["model_integrity_ok"])
            _cleanup_temp_dir(dest_dir)
            continue

        # ── Download data repo (if any) ──
        if gm.hf_data_repo_id:
            d_ok, d_msg, _ = _download_data_repo(
                gm.hf_data_repo_id,
                dest_dir,
                token=token,
            )
            if not d_ok:
                log.warning(
                    "Data download failed for user=%s game=%s: %s",
                    user_id, game_type, d_msg,
                )

        # ── Scan model ──
        scan_passed, report = scan_model(model_dir)

        # Build current file list for diffing
        current_file_list = _build_file_list(model_dir)
        report["file_list"] = current_file_list

        # ── Compare commit SHA ──
        current_sha = _get_current_commit_sha(gm.hf_model_repo_id, token)

        if not scan_passed:
            # Scan failed — block and delete
            log.warning(
                "Scan FAILED for user=%s game=%s repo=%s",
                user_id, game_type, gm.hf_model_repo_id,
            )
            gm.model_integrity_ok = False
            gm.scan_report = report
            gm.save(update_fields=["model_integrity_ok", "scan_report"])
            _cleanup_temp_dir(dest_dir)
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
            gm.save(update_fields=[
                "model_integrity_ok",
                "scan_report",
                "last_verified_commit",
                "last_verified_at",
                "last_model_validation_date",
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

    # Clear cache keys for every game type this user has
    game_types = (
        UserGameModel.objects
        .filter(user_id=user_id)
        .values_list("game_type", flat=True)
    )
    for gt in game_types:
        cache.delete(f"model_status_{user_id}_{gt}")

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
