"""Download, size-check and validate a guest's public Hugging Face repos.

Guests never get the platform token or the shared unrestricted download path:
every request is anonymous, pinned to one commit SHA, and limited to the exact
files that were size-checked for that commit.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_LOCK_SECONDS = 1800


class GuestRepoError(Exception):
    """A problem with a guest's repo that is safe to show to the guest."""


@dataclass(frozen=True)
class RepoListing:
    sha: str
    files: tuple  # ((relative name, size in bytes), ...)

    @property
    def total_bytes(self) -> int:
        return sum(size for _name, size in self.files)


# ── Validation ────────────────────────────────────────────────────────────────
def validate_repo_id(repo_id: str, hf_username: str, label: str) -> None:
    """Shape and namespace rules. Raises GuestRepoError."""
    if not repo_id or not _REPO_ID_RE.match(repo_id):
        raise GuestRepoError(f"The {label} repo ID must look like 'YourName/repo-name'.")
    namespace, name = repo_id.split("/", 1)
    if "--" in repo_id:
        raise GuestRepoError(f"The {label} repo ID may not contain '--'. Rename the repo on Hugging Face first.")
    if namespace in (".", "..") or name in (".", ".."):
        raise GuestRepoError(f"The {label} repo ID is not valid.")
    if namespace.lower() != (hf_username or "").lower():
        raise GuestRepoError(
            f"The {label} repo must be under your own Hugging Face account ('{hf_username}/...')."
        )


def check_not_used_by_another_account(user, kind: str, repo_id: str) -> None:
    from apps.users.models import CustomUser, UserGameModel

    field = "hf_model_repo_id" if kind == "model" else "hf_data_repo_id"
    taken = UserGameModel.objects.filter(**{f"{field}__iexact": repo_id}).exclude(user=user).exists()
    if not taken and kind == "model":
        taken = CustomUser.objects.filter(hf_model_repo_id__iexact=repo_id).exclude(pk=user.pk).exists()
    if taken:
        raise GuestRepoError(f"The {kind} repo '{repo_id}' is already registered by another account.")


def _safe_name(name: str) -> bool:
    return bool(name) and not name.startswith("/") and "\\" not in name and ".." not in name.split("/")


def fetch_listing(repo_id: str, repo_type: str = "model", revision: str | None = None) -> RepoListing:
    """Anonymous listing of *repo_id* at *revision* (latest commit when None).

    Raises GuestRepoError when the repo is missing, private or gated.
    """
    from huggingface_hub import HfApi
    from huggingface_hub.utils import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

    kind = "data" if repo_type == "dataset" else "model"
    api = HfApi()
    try:
        if revision is None:
            head = api.repo_info(repo_id, repo_type=repo_type, expand=["gated", "private", "sha"], token=False)
            if getattr(head, "gated", False):
                raise GuestRepoError(f"The {kind} repo '{repo_id}' is gated. Guests need a public repo.")
            if getattr(head, "private", False):
                raise GuestRepoError(f"The {kind} repo '{repo_id}' is private. Your repos must be public.")
            revision = head.sha
        info = api.repo_info(repo_id, repo_type=repo_type, revision=revision, files_metadata=True, token=False)
    except GuestRepoError:
        raise
    except GatedRepoError:
        raise GuestRepoError(f"The {kind} repo '{repo_id}' is gated. Guests need a public repo.")
    except RepositoryNotFoundError:
        raise GuestRepoError(
            f"The {kind} repo '{repo_id}' was not found. Check the ID; your repos must be public."
        )
    except HfHubHTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            raise GuestRepoError(f"Revision {str(revision)[:12]} of '{repo_id}' no longer exists.")
        log.warning("HF listing failed for %s: status=%s", repo_id, status)
        raise GuestRepoError("Could not reach Hugging Face. Please try again in a moment.")

    files = []
    for sibling in info.siblings or []:
        name, size = sibling.rfilename, sibling.size
        if not _safe_name(name):
            raise GuestRepoError(f"The {kind} repo contains an unsupported file name.")
        if size is None:
            raise GuestRepoError(f"Could not determine the size of '{name}' in '{repo_id}'.")
        files.append((name, int(size)))
    return RepoListing(sha=info.sha or revision, files=tuple(files))


def check_limits(listing: RepoListing, repo_id: str) -> None:
    max_bytes = settings.GUEST_MAX_REPO_MB * 1024 * 1024
    if not listing.files:
        raise GuestRepoError(f"'{repo_id}' has no files.")
    if len(listing.files) > settings.GUEST_MAX_REPO_FILES:
        raise GuestRepoError(
            f"'{repo_id}' has {len(listing.files)} files; guests may use at most {settings.GUEST_MAX_REPO_FILES}."
        )
    if listing.total_bytes > max_bytes:
        raise GuestRepoError(
            f"'{repo_id}' is {listing.total_bytes // (1024 * 1024)} MB; "
            f"guests may use at most {settings.GUEST_MAX_REPO_MB} MB per repo."
        )


# ── Download ──────────────────────────────────────────────────────────────────
def _patterns(listing: RepoListing) -> list[str]:
    return [re.sub(r"([*?\[])", r"[\1]", name) for name, _size in listing.files]


def data_cache_dir(user_id: int, game_type: str, repo_id: str, sha: str) -> Path:
    from apps.games.local_inference import _dataset_cache_folder_name, _models_base

    return _models_base() / f"user_{user_id}" / game_type / "data_cache" / _dataset_cache_folder_name(repo_id) / sha


def download_model(repo_id: str, listing: RepoListing) -> Path:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo_id, repo_type="model", revision=listing.sha,
        cache_dir=str(settings.HF_HUB_CACHE), allow_patterns=_patterns(listing), token=False,
    )
    from apps.games.local_inference import _find_hf_cache_snapshot

    snap = _find_hf_cache_snapshot(repo_id, ref=listing.sha)
    if snap is None or snap.name.lower() != listing.sha.lower():
        raise GuestRepoError("The model download did not complete. Please try again.")
    return snap


def download_data(user_id: int, game_type: str, repo_id: str, listing: RepoListing) -> Path:
    from huggingface_hub import snapshot_download

    dest = data_cache_dir(user_id, game_type, repo_id, listing.sha)
    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id, repo_type="dataset", revision=listing.sha, local_dir=str(dest),
        local_dir_use_symlinks=False, allow_patterns=_patterns(listing), token=False,
    )
    return dest


# ── Readiness ─────────────────────────────────────────────────────────────────
def guest_snapshot_ready(user, game_type: str) -> tuple[bool, str]:
    """(ready, reason). A guest may start a game only when this is True."""
    from apps.games.local_inference import _find_hf_cache_snapshot, _has_model_files, _has_real_data_files
    from apps.users.models import UserGameModel

    gm = user.get_game_model(game_type)
    if gm is None or not (gm.hf_model_repo_id or "").strip():
        return False, f"Connect a public {game_type} repo in the Gladiator tab first."
    if gm.status == UserGameModel.ContractStatus.FAILED:
        return False, gm.last_error or "Your model check failed. See the Gladiator tab."
    sha = (gm.approved_full_sha or "").strip()
    if gm.status != UserGameModel.ContractStatus.ACTIVE or not sha:
        return False, gm.last_error or "Preparing your model..."
    snap = _find_hf_cache_snapshot(gm.hf_model_repo_id, ref=sha)
    if snap is None or snap.name.lower() != sha.lower() or not _has_model_files(snap):
        return False, gm.last_error or "Preparing your model..."
    data_repo = (gm.hf_data_repo_id or "").strip()
    if data_repo:
        data_sha = (gm.approved_data_repo_sha or "").strip()
        if not data_sha or not _has_real_data_files(data_cache_dir(user.pk, game_type, data_repo, data_sha)):
            return False, gm.last_error or "Preparing your model..."
    return True, "OK"


# ── Preparation (runs in Celery, or a thread when no broker is available) ─────
def _set_error(gm, message: str, *, failed: bool) -> None:
    from apps.users.models import UserGameModel

    gm.last_error = message[:900]
    fields = ["last_error"]
    if failed:
        gm.status = UserGameModel.ContractStatus.FAILED
        fields.append("status")
    gm.save(update_fields=fields)


def prepare_guest_snapshot(gm_id: int, model_sha: str | None = None, data_sha: str | None = None, *, pin: bool = True) -> None:
    """Download and check a guest's repos. ``pin=True`` pins the commit once the check passes.

    With ``pin=False`` the already pinned commit is downloaded again (next login, after the
    idle clean-up). Never raises.
    """
    from apps.users.integrity import record_original_sha
    from apps.users.models import UserGameModel

    lock_key = f"guest-prepare:{gm_id}"
    if not cache.add(lock_key, 1, timeout=_LOCK_SECONDS):
        return
    try:
        gm = UserGameModel.objects.select_related("user").get(pk=gm_id)
        if not gm.user.is_guest:
            return
        try:
            model_repo = gm.hf_model_repo_id
            model_listing = fetch_listing(model_repo, "model", revision=model_sha)
            check_limits(model_listing, model_repo)
            data_repo = (gm.hf_data_repo_id or "").strip()
            data_listing = None
            if data_repo:
                data_listing = fetch_listing(data_repo, "dataset", revision=data_sha)
                check_limits(data_listing, data_repo)

            snap = download_model(model_repo, model_listing)
            if data_listing is not None:
                download_data(gm.user_id, gm.game_type, data_repo, data_listing)

            now = timezone.now()
            gm.cached_path, gm.cached_commit, gm.cached_at = str(snap), model_listing.sha, now
            gm.save(update_fields=["cached_path", "cached_commit", "cached_at"])

            if pin:
                record_original_sha(
                    gm, False, sha=model_listing.sha,
                    data_sha=data_listing.sha if data_listing is not None else None,
                )
                gm.refresh_from_db()
                if gm.status == UserGameModel.ContractStatus.PENDING:
                    _set_error(gm, "The model check could not run right now. Click 'Check again' in a minute.", failed=False)
            elif gm.last_error:
                gm.last_error = ""
                gm.save(update_fields=["last_error"])
        except GuestRepoError as exc:
            _set_error(gm, str(exc), failed=pin)
        except Exception:
            log.exception("Guest snapshot preparation failed for gm=%s", gm_id)
            _set_error(gm, "Something went wrong while preparing your model. Please try again.", failed=False)
    finally:
        cache.delete(lock_key)


def schedule_guest_prepare(gm_id: int, model_sha: str | None = None, data_sha: str | None = None, *, pin: bool = True) -> None:
    """Queue the preparation on GUEST_CELERY_QUEUE; fall back to a thread without a broker."""
    try:
        from apps.users.tasks import prepare_guest_snapshot_task

        prepare_guest_snapshot_task.apply_async(
            args=[gm_id, model_sha, data_sha, pin], queue=settings.GUEST_CELERY_QUEUE,
        )
        return
    except Exception:
        log.warning("Could not enqueue guest preparation for gm=%s; running it in a thread", gm_id, exc_info=True)
    threading.Thread(
        target=prepare_guest_snapshot, args=(gm_id, model_sha, data_sha), kwargs={"pin": pin},
        daemon=True, name=f"guest-prepare-{gm_id}",
    ).start()


def prepare_pinned_snapshots(user) -> None:
    """At guest login: re-download every pinned snapshot that is missing."""
    from apps.users.models import UserGameModel

    for gm in user.game_models.exclude(approved_full_sha=""):
        if gm.status == UserGameModel.ContractStatus.FAILED:
            continue
        ready, _ = guest_snapshot_ready(user, gm.game_type)
        if not ready:
            schedule_guest_prepare(
                gm.pk, gm.approved_full_sha, (gm.approved_data_repo_sha or None), pin=False,
            )


def _is_preparing(gm_id: int) -> bool:
    return bool(cache.get(f"guest-prepare:{gm_id}"))


def prepare_guest_on_login(user) -> None:
    """Guest counterpart of preload_user_models: at login, fetch every connected repo that is not ready.

    A passed, pinned model comes back at its pinned commit. One that never passed, or failed, is
    fetched at the latest commit and checked again once both downloads are done.
    """
    from apps.users.models import UserGameModel

    for gm in user.game_models.exclude(hf_model_repo_id=""):
        if _is_preparing(gm.pk) or guest_snapshot_ready(user, gm.game_type)[0]:
            continue
        sha = (gm.approved_full_sha or "").strip()
        if sha and gm.status == UserGameModel.ContractStatus.ACTIVE:
            schedule_guest_prepare(gm.pk, sha, (gm.approved_data_repo_sha or None), pin=False)
            continue
        UserGameModel.objects.filter(pk=gm.pk).update(status=UserGameModel.ContractStatus.PENDING, last_error="")
        schedule_guest_prepare(gm.pk, None, None, pin=True)


def ensure_guest_snapshots(user) -> None:
    """Re-download missing pinned snapshots (e.g. after the idle clean-up); at most once a minute."""
    if cache.add(f"guest-ensure:{user.pk}", 1, timeout=60):
        prepare_pinned_snapshots(user)
