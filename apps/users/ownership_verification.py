# ──────────────────────────────────────────────
# apps/users/ownership_verification.py
#
# Proof-of-Ownership verification via AGL_VERIFY.txt.
#
# Flow
# ────
# 1. generate_verification_code(game_model)
#    → generates a 32-char random hex code, saves it to the
#      UserGameModel record, returns the code.
#
# 2. User adds AGL_VERIFY.txt to the root of their HF repo
#    containing exactly that code (no extra whitespace).
#
# 3. check_ownership(game_model)
#    → downloads AGL_VERIFY.txt from the public HF repo,
#      compares contents, marks is_verified=True on success.
#
# 4. re_verify_ownership(game_model)
#    → same check without regenerating the code.  Used by
#      mid-round random spot-checks.
#
# 5. snapshot_repo_commit_time(game_model)
#    → records the latest commit timestamp at tournament
#      registration time so pre-round checks can detect
#      post-registration pushes.
#
# No HF token is required. The repo must be public.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.users.models import UserGameModel

log = logging.getLogger(__name__)

VERIFY_FILENAME = "AGL_VERIFY.txt"
# 32 hex chars = 128 bits of entropy
CODE_HEX_BYTES = 16


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_verification_code(game_model: "UserGameModel") -> str:
    """Generate a fresh challenge code, persist it, and return it.

    Resets ``is_verified`` to False so the user must complete the
    file-upload step again if they call this more than once.
    """
    code = secrets.token_hex(CODE_HEX_BYTES)
    game_model.verification_code = code
    game_model.verification_code_issued_at = timezone.now()
    game_model.is_verified = False
    game_model.save(update_fields=[
        "verification_code",
        "verification_code_issued_at",
        "is_verified",
    ])
    log.info(
        "Verification code issued for user=%s game=%s repo=%s",
        game_model.user_id, game_model.game_type, game_model.hf_model_repo_id,
    )
    return code


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ownership check (first-time and re-verify)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_ownership(game_model: "UserGameModel") -> tuple[bool, str]:
    """Verify ownership by downloading AGL_VERIFY.txt from the HF repo.

    The repo must be public — no HF token is used.

    Returns ``(success, message)``.
    On success: sets ``is_verified=True``, ``verified_at=now()``, and
    snapshots the latest commit timestamp for pre-round change detection.
    """
    repo_id = game_model.hf_model_repo_id
    expected_code = game_model.verification_code

    if not repo_id:
        return False, "No repository linked to this game model."
    if not expected_code:
        return False, (
            "No verification code has been issued yet. "
            "Please generate a code first, then add it to your repo."
        )

    content, error = _fetch_verify_file(repo_id)
    if error:
        return False, error

    if content != expected_code:
        return False, (
            f"{VERIFY_FILENAME} content does not match the expected code. "
            "Ensure the file contains exactly the verification code with no "
            "extra whitespace or newlines."
        )

    # ── Mark verified ──────────────────────────
    now = timezone.now()
    update_fields = ["is_verified", "verified_at"]
    game_model.is_verified = True
    game_model.verified_at = now

    # Snapshot repo commit time for pre-round change detection
    commit_time = _get_latest_commit_time(repo_id)
    if commit_time:
        game_model.repo_last_modified_at_registration = commit_time
        update_fields.append("repo_last_modified_at_registration")

    game_model.save(update_fields=update_fields)
    log.info(
        "Ownership verified for user=%s game=%s repo=%s",
        game_model.user_id, game_model.game_type, repo_id,
    )
    return True, "Ownership verified successfully."


def re_verify_ownership(game_model: "UserGameModel") -> tuple[bool, str]:
    """Re-run the AGL_VERIFY.txt check without regenerating the code.

    Used by mid-round random spot-checks.
    Returns ``(still_ok, message)``.
    If the file is missing or code mismatches, sets ``is_verified=False``.
    """
    repo_id = game_model.hf_model_repo_id
    expected_code = game_model.verification_code

    if not repo_id or not expected_code:
        # Not configured for ownership verification — treat as pass
        return True, "Ownership verification not configured — skipping."

    content, error = _fetch_verify_file(repo_id)
    if error:
        # Network/API unavailable — do not penalise the user
        log.warning(
            "re_verify_ownership: could not fetch %s from %s: %s",
            VERIFY_FILENAME, repo_id, error,
        )
        return True, f"HF API unavailable — skipping re-verify: {error}"

    if content != expected_code:
        _mark_unverified(game_model, "AGL_VERIFY.txt code mismatch or file removed")
        return False, (
            f"{VERIFY_FILENAME} code changed or file was removed — "
            "ownership no longer confirmed."
        )

    return True, "Ownership re-verified OK."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Repo timestamp snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def snapshot_repo_commit_time(game_model: "UserGameModel") -> None:
    """Record the repo's latest commit timestamp on the game_model.

    Call this at tournament registration time so pre-round checks can
    detect if the repo was pushed to after registration.
    """
    repo_id = game_model.hf_model_repo_id
    if not repo_id:
        return
    commit_time = _get_latest_commit_time(repo_id)
    if commit_time:
        game_model.repo_last_modified_at_registration = commit_time
        game_model.save(update_fields=["repo_last_modified_at_registration"])
        log.info(
            "Snapshotted repo commit time for user=%s game=%s: %s",
            game_model.user_id, game_model.game_type, commit_time,
        )


def has_repo_changed_since_registration(game_model: "UserGameModel") -> bool:
    """Return True if the repo has a newer commit than the registration snapshot.

    Used by the pre-round ownership check task.
    Returns False if no snapshot exists (cannot determine) or on error.
    """
    repo_id = game_model.hf_model_repo_id
    baseline = game_model.repo_last_modified_at_registration
    if not repo_id or not baseline:
        return False

    latest = _get_latest_commit_time(repo_id)
    if latest is None:
        return False

    # Make both timezone-aware for comparison
    import django.utils.timezone as dj_tz
    if dj_tz.is_naive(latest):
        latest = dj_tz.make_aware(latest)
    if dj_tz.is_naive(baseline):
        baseline = dj_tz.make_aware(baseline)

    return latest > baseline


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _platform_token() -> str:
    """Return the platform HF token (ArtificialGladiatorLeague account), or empty string."""
    from django.conf import settings
    return getattr(settings, "HF_PLATFORM_TOKEN", "") or ""


def _fetch_verify_file(repo_id: str) -> tuple[str | None, str | None]:
    """Download AGL_VERIFY.txt from *repo_id* using the platform token.

    Uses a direct HTTP GET against the HF resolve endpoint so we never hit
    the local huggingface_hub cache (avoids stale reads and Windows file-lock
    issues triggered by ``force_download=True``).

    Gated repos (access-restricted) are supported: the platform account
    ArtificialGladiatorLeague authenticates with HF_PLATFORM_TOKEN.
    Users must still grant access to ArtificialGladiatorLeague on their repo.

    Returns ``(content, None)`` on success or ``(None, error_message)`` on failure.
    """
    import requests

    token = _platform_token()
    headers: dict[str, str] = {"Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://huggingface.co/{repo_id}/resolve/main/{VERIFY_FILENAME}"
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
    except requests.RequestException as exc:
        log.warning("_fetch_verify_file: network error for %s: %s", repo_id, exc)
        return None, f"Could not reach Hugging Face to fetch {VERIFY_FILENAME}: {exc}"

    if resp.status_code == 404:
        return None, (
            f"{VERIFY_FILENAME} was not found in your repo '{repo_id}'. "
            "Please add the file at the root of the repo and try again."
        )
    if resp.status_code in (401, 403):
        return None, (
            f"Repository '{repo_id}' rejected our access request (HTTP {resp.status_code}). "
            "Make sure your repo is set to Gated and that you have granted access to the "
            "'ArtificialGladiatorLeague' account on Hugging Face."
        )
    if resp.status_code >= 400:
        return None, (
            f"Could not download {VERIFY_FILENAME} from '{repo_id}' "
            f"(HTTP {resp.status_code}). Please check that the repo exists and is accessible."
        )

    return resp.text.strip(), None


def _get_latest_commit_time(repo_id: str):
    """Return the ``created_at`` datetime of the latest commit, or None."""
    try:
        from huggingface_hub import list_repo_commits

        commits = list(list_repo_commits(repo_id, repo_type="model", token=None))
        if commits:
            return commits[0].created_at
    except Exception as exc:
        log.debug("Could not fetch commit time for %s: %s", repo_id, exc)
    return None


def _mark_unverified(game_model: "UserGameModel", reason: str) -> None:
    log.warning(
        "Ownership invalidated for user=%s game=%s repo=%s: %s",
        game_model.user_id, game_model.game_type,
        game_model.hf_model_repo_id, reason,
    )
    game_model.is_verified = False
    game_model.save(update_fields=["is_verified"])
