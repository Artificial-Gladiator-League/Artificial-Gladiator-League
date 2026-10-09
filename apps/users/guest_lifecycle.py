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


FORCE_AFTER = timedelta(days=1)


def expired_guest_ids(now=None) -> list[int]:
    """Guests whose last-activity expiry has passed. Platform accounts are never listed."""
    from django.contrib.auth import get_user_model

    now = now or timezone.now()
    created_cutoff = now - timedelta(days=settings.GUEST_TTL_DAYS)
    qs = get_user_model().objects.filter(
        Q(guest_expires_at__lt=now) | Q(guest_expires_at__isnull=True, guest_created_at__lt=created_cutoff),
        is_guest=True, is_official_bot=False,
    )
    return list(qs.order_by("pk").values_list("pk", flat=True))


def purge_expired_guests(now=None, *, dry_run: bool = False) -> list[dict]:
    """Delete guests that were idle for GUEST_TTL_DAYS (account, repos, files). Never raises.

    A guest in an ongoing game is skipped until a day past expiry; after that the stale
    games are aborted so the account cannot linger forever. Independent of
    ``cleanup_orphaned_dirs``.
    """
    from django.contrib.auth import get_user_model

    from apps.games.models import Game
    from apps.users.model_lifecycle import _user_ids_in_ongoing_games
    from apps.users.models import GuestUpgradeRequest

    now = now or timezone.now()
    User = get_user_model()
    report: list[dict] = []
    try:
        busy = _user_ids_in_ongoing_games()
        for user_id in expired_guest_ids(now):
            user = User.objects.filter(pk=user_id).only("username", "guest_expires_at").first()
            if user is None:
                continue
            entry = {"id": user_id, "username": user.username, "expired": user.guest_expires_at, "action": "delete"}
            if user_id in busy:
                stale = user.guest_expires_at is not None and user.guest_expires_at < now - FORCE_AFTER
                if not stale:
                    entry["action"] = "skip (ongoing game)"
                    report.append(entry)
                    continue
                entry["action"] = "abort games and delete"
                if not dry_run:
                    Game.objects.filter(
                        Q(white_id=user_id) | Q(black_id=user_id),
                        status__in=[Game.Status.ONGOING, Game.Status.WAITING],
                    ).update(status=Game.Status.ABORTED, result_reason="guest expired")
            if not dry_run and not delete_guest_account(user_id):
                entry["action"] = "failed"
            report.append(entry)
        if not dry_run:
            GuestUpgradeRequest.objects.filter(expires_at__lt=now - timedelta(days=7)).delete()
    except Exception:
        log.exception("Guest purge failed")
    return report
