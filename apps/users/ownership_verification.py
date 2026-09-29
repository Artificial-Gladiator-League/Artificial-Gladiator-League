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
#  Data repo ownership check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_data_repo_ownership(game_model: "UserGameModel") -> "tuple[bool, str]":
    """Verify ownership of the linked HF data repo by checking AGL_VERIFY.txt.

    The data repo must contain AGL_VERIFY.txt at its root with content
    identical to game_model.verification_code (the same single code used
    for both ownership checks).

    Returns ``(success, message)``.
    """
    data_repo_id = game_model.hf_data_repo_id
    expected_code = game_model.verification_code

    if not data_repo_id:
        # No data repo linked — skip silently (not required for all game types).
        return True, "No data repo linked — skipping data repo check."
    if not expected_code:
        return False, (
            "No verification code has been issued yet. "
            "Please save the repo first to generate a code."
        )

    content, error = _fetch_verify_file(data_repo_id, repo_type="dataset")
    if error:
        return False, f"Data repo check failed: {error}"

    if content != expected_code:
        return False, (
            f"{VERIFY_FILENAME} in data repo '{data_repo_id}' does not match "
            "the expected code. Ensure the file contains exactly the verification "
            "code with no extra whitespace or newlines."
        )

    log.info(
        "Data repo ownership verified for user=%s game=%s data_repo=%s",
        game_model.user_id, game_model.game_type, data_repo_id,
    )
    return True, f"Data repo '{data_repo_id}' ownership verified."


def check_full_ownership(game_model: "UserGameModel") -> "tuple[bool, str]":
    """Full two-way ownership check: model repo + data repo.

    Checks are run in sequence:
      1. Model repo — AGL_VERIFY.txt == verification_code  (always required)
      2. Data repo  — AGL_VERIFY.txt == verification_code  (if data repo set)

    Per-field booleans (model_repo_ownership_verified, data_repo_ownership_verified)
    are reset at the start and then set to True as each step passes, so the
    template can show exactly which repos are verified.

    Returns ``(False, human-readable error message)`` on any failure, or
    ``(True, summary message)`` on full success.
    """
    from apps.users.models import UserGameModel as _UGM

    expected_code = game_model.verification_code
    if not expected_code:
        return False, (
            "No verification code has been issued yet. "
            "Please save the repo (Connect Repo) first to generate a code."
        )

    # Reset all per-field flags at the start of each verification run.
    _UGM.objects.filter(pk=game_model.pk).update(
        model_repo_ownership_verified=False,
        data_repo_ownership_verified=False,
    )
    game_model.model_repo_ownership_verified = False
    game_model.data_repo_ownership_verified = False

    # ── 1. Model repo ──────────────────────────────────────────────────
    repo_ok, repo_msg = check_ownership(game_model)
    if not repo_ok:
        log.warning(
            "check_full_ownership: model repo FAILED for user=%s game=%s repo=%s",
            game_model.user_id, game_model.game_type, game_model.hf_model_repo_id,
        )
        return False, f"Model repo check failed: {repo_msg}"

    _UGM.objects.filter(pk=game_model.pk).update(model_repo_ownership_verified=True)
    game_model.model_repo_ownership_verified = True

    # ── 2. Data repo (independent — does NOT block model-repo verification) ─
    pending_failures: list[str] = []
    data_repo_id = game_model.hf_data_repo_id
    if data_repo_id:
        data_ok, data_msg = check_data_repo_ownership(game_model)
        if data_ok:
            _UGM.objects.filter(pk=game_model.pk).update(data_repo_ownership_verified=True)
            game_model.data_repo_ownership_verified = True
        else:
            log.warning(
                "check_full_ownership: data repo not yet verified for user=%s game=%s data_repo=%s: %s",
                game_model.user_id, game_model.game_type, data_repo_id, data_msg,
            )
            pending_failures.append(f"Data repo: {data_msg}")

    # ── Final status ────────────────────────────────────────────────────
    import html as _html

    def _row(n: int, ok: bool, label: str, detail: str, extra: str = "") -> str:
        icon = "✅" if ok else "❌"
        status = "verified" if ok else detail
        suffix = f" — {_html.escape(extra)}" if extra else ""
        return f"{n}. {icon} <strong>{_html.escape(label)}</strong>{suffix} — {status}"

    # Build a full numbered list in fixed order: model → data repo.
    items: list[str] = []
    n = 1
    items.append(_row(n, True, "Model repo",
                       "verified", game_model.hf_model_repo_id))
    n += 1
    if data_repo_id:
        data_verified = game_model.data_repo_ownership_verified
        data_fail_msg = next(
            (f.replace("Data repo: ", "", 1) for f in pending_failures if f.startswith("Data repo:")),
            "not yet verified — add AGL_VERIFY.txt to the data repo",
        )
        items.append(_row(n, data_verified, "Data repo",
                           data_fail_msg, data_repo_id))

    body = "<br>".join(items)

    if pending_failures:
        # Model repo is verified; data repo still needs AGL_VERIFY.txt.
        log.info(
            "check_full_ownership: model repo PASSED but pending items for user=%s game=%s: %s",
            game_model.user_id, game_model.game_type, " | ".join(pending_failures),
        )
        return False, "<strong>Ownership check results:</strong><br>" + body

    # ── All passed ─────────────────────────────────────────────────────
    # NOTE: SHA pinning is intentionally NOT done here — apps.users.integrity
    # .record_original_sha() is the sole place that pins approved_full_sha,
    # since it only does so after check_model() passes. Pinning it here too
    # would let a SHA become "approved" without ever running the contract
    # check (see views.py::_handle_ai_model_post, which calls
    # record_original_sha() right after this function returns True).
    log.info(
        "check_full_ownership: all checks PASSED for user=%s game=%s",
        game_model.user_id, game_model.game_type,
    )
    return True, "<strong>Ownership check results:</strong><br>" + body


def _rollback_verified(game_model: "UserGameModel") -> None:
    """Revert is_verified=False after a partial ownership failure."""
    game_model.is_verified = False
    game_model.save(update_fields=["is_verified"])



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _platform_token() -> str:
    """Return the platform HF token (ArtificialGladiatorLeague account), or empty string."""
    from django.conf import settings
    return getattr(settings, "HF_PLATFORM_TOKEN", "") or ""


def _fetch_verify_file(repo_id: str, repo_type: str = "model") -> tuple[str | None, str | None]:
    """Download AGL_VERIFY.txt from *repo_id* using the platform token.

    Uses a direct HTTP GET against the HF resolve endpoint so we never hit
    the local huggingface_hub cache (avoids stale reads and Windows file-lock
    issues triggered by ``force_download=True``).

    Gated repos (access-restricted) are supported: the platform account
    ArtificialGladiatorLeague authenticates with HF_PLATFORM_TOKEN.
    Users must still grant access to ArtificialGladiatorLeague on their repo.

    *repo_type* must be ``"model"`` (default) or ``"dataset"``.  Dataset repos
    live under ``huggingface.co/datasets/`` on the HF CDN; model repos live
    directly under ``huggingface.co/``.

    Returns ``(content, None)`` on success or ``(None, error_message)`` on failure.
    """
    import requests

    token = _platform_token()
    headers: dict[str, str] = {"Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if repo_type == "dataset":
        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{VERIFY_FILENAME}"
    else:
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


def _get_latest_commit_time(repo_id: str, repo_type: str = "model"):
    """Return the ``created_at`` datetime of the latest commit, or None."""
    try:
        from huggingface_hub import list_repo_commits
        from huggingface_hub.utils import HfHubHTTPError, RepositoryNotFoundError

        try:
            commits = list(list_repo_commits(repo_id, repo_type=repo_type, token=None))
        except RepositoryNotFoundError:
            log.debug("_get_latest_commit_time: %s not found as repo_type=%s", repo_id, repo_type)
            return None
        except HfHubHTTPError as exc:
            log.debug("_get_latest_commit_time: HF API error for %s: %s", repo_id, exc)
            return None
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
