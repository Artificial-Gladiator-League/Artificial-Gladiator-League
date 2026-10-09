"""Guest snapshot and account clean-up (logout, idle, delete-my-data, purge)."""
from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

log = logging.getLogger(__name__)


def is_protected_account(user) -> bool:
    """Accounts no guest clean-up may touch (e.g. a future platform-owned official bot)."""
    return bool(getattr(user, "is_official_bot", False))


def delete_guest_snapshots(user_id: int) -> bool:
    """Delete a guest's downloaded files; keep repo ids and pinned SHAs.

    Removes the user's folder (model/data caches) and the shared HF-cache folders that no
    other account uses. Does nothing while the guest is in an ongoing game. Never raises.
    Returns True when the files were deleted.
    """
    try:
        from apps.games.model_preloader import clear_user_models
        from apps.users.model_lifecycle import _user_ids_in_ongoing_games, delete_hf_cache_for_user
        from apps.users.models import CustomUser, UserGameModel

        owner = CustomUser.objects.filter(pk=user_id).first()
        if owner is not None and is_protected_account(owner):
            return False
        if user_id in _user_ids_in_ongoing_games():
            log.info("Guest snapshot cleanup skipped: user=%s is in an ongoing game", user_id)
            return False
        delete_hf_cache_for_user(user_id)
        clear_user_models(user_id)
        from apps.games.sandbox_runner import guest_real_dir_root
        from apps.users.model_lifecycle import _hf_cache_folder_name

        for repo_id in UserGameModel.objects.filter(user_id=user_id).values_list("hf_model_repo_id", flat=True):
            if repo_id:
                shutil.rmtree(guest_real_dir_root() / _hf_cache_folder_name("models", repo_id.strip()), ignore_errors=True)
        UserGameModel.objects.filter(user_id=user_id).update(cached_path="", cached_commit="", cached_at=None)
        log.info("Guest snapshots deleted for user=%s", user_id)
        return True
    except Exception:
        log.exception("Guest snapshot cleanup failed for user=%s", user_id)
        return False


def cleanup_idle_guest_snapshots() -> int:
    """Delete the snapshots of guests idle for GUEST_SNAPSHOT_IDLE_MINUTES. Returns how many."""
    from django.contrib.auth import get_user_model

    cutoff = timezone.now() - timedelta(minutes=settings.GUEST_SNAPSHOT_IDLE_MINUTES)
    idle = get_user_model().objects.filter(
        Q(guest_last_seen__lt=cutoff) | Q(guest_last_seen__isnull=True, guest_created_at__lt=cutoff),
        is_guest=True, is_official_bot=False, game_models__cached_path__gt="",
    ).distinct()
    cleaned = 0
    for user_id in list(idle.values_list("pk", flat=True)):
        if delete_guest_snapshots(user_id):
            cleaned += 1
    return cleaned


def delete_guest_account(user_id: int) -> bool:
    """Delete a guest account and all its files. Never raises; False when it was skipped."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.filter(pk=user_id, is_guest=True).first()
        if user is None or is_protected_account(user):
            return False
        from apps.users.model_lifecycle import _user_ids_in_ongoing_games

        if user_id in _user_ids_in_ongoing_games():
            log.info("Guest account %s not deleted: ongoing game", user_id)
            return False
        delete_guest_snapshots(user_id)
        live_root = getattr(settings, "LIVE_MODELS_ROOT", None)
        if live_root:
            shutil.rmtree(Path(live_root) / f"user_{user_id}", ignore_errors=True)
        user.delete()
        log.info("Guest account %s deleted", user_id)
        return True
    except Exception:
        log.exception("Could not delete guest account %s", user_id)
        return False
