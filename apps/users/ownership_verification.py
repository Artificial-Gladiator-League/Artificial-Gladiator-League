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
#  Space ownership check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _parse_space_url_subdomain(space_url: str) -> "tuple[str | None, str | None]":
    """Extract the raw subdomain from an hf.space URL.

    Returns ``(subdomain, None)`` on success or ``(None, error_message)`` on failure.
    """
    import re
    m = re.match(r'^https://([a-zA-Z0-9][a-zA-Z0-9._-]*)\.hf\.space/?$', space_url.strip())
    if not m:
        return None, (
            f"Cannot parse Space URL '{space_url}'. "
            "Expected format: https://owner-spacename.hf.space"
        )
    subdomain = m.group(1)
    if '-' not in subdomain:
        return None, (
            f"Cannot determine Space owner from subdomain '{subdomain}'. "
            "URL must be in the form https://owner-spacename.hf.space"
        )
    return subdomain, None


def _fetch_verify_file_from_space(subdomain: str) -> "tuple[str | None, str | None, str | None]":
    """Download AGL_VERIFY.txt from an HF Space, trying every possible owner/space split.

    HF Space subdomains are ``{owner}-{space-name}`` where the ``/`` separator is
    replaced by ``-``.  Because both the owner name and the space name may themselves
    contain hyphens the split position is ambiguous.  This function probes each
    candidate split (left-to-right) and returns the first successful result.

    Returns ``(repo_id, content, None)`` on success or ``(None, None, error_message)``
    when no split produces a readable file.
    """
    import requests

    token = _platform_token()
    headers: dict[str, str] = {"Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    parts = subdomain.split('-')
    last_error: str = f"{VERIFY_FILENAME} was not found in the Space at '{subdomain}.hf.space'."

    for i in range(1, len(parts)):
        owner = '-'.join(parts[:i])
        space_name = '-'.join(parts[i:])
        repo_id = f"{owner}/{space_name}"
        url = f"https://huggingface.co/spaces/{repo_id}/resolve/main/{VERIFY_FILENAME}"
        try:
            resp = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
        except requests.RequestException as exc:
            log.warning(
                "_fetch_verify_file_from_space: network error trying %s: %s",
                repo_id, exc,
            )
            last_error = f"Could not reach Hugging Face to fetch {VERIFY_FILENAME}: {exc}"
            continue

        if resp.status_code == 200:
            log.info("_fetch_verify_file_from_space: found file at Space repo %s", repo_id)
            return repo_id, resp.text.strip(), None
        if resp.status_code in (401, 403):
            last_error = (
                f"Space '{repo_id}' rejected our access request "
                f"(HTTP {resp.status_code}). Make sure the Space is public."
            )
            # A permission error means we found the right repo but can't read it — stop.
            break
        # 404 or other error: try next split
        log.debug(
            "_fetch_verify_file_from_space: HTTP %s for %s, trying next split",
            resp.status_code, repo_id,
        )

    return None, None, last_error


def check_space_ownership(game_model: "UserGameModel") -> "tuple[bool, str]":
    """Verify ownership of the linked HF Space by checking AGL_VERIFY.txt.

    Kept as a standalone helper.  Prefer ``check_full_ownership`` for the
    full three-way check (model repo + Space + data repo).
    """
    space_url = game_model.hf_inference_endpoint_url
    expected_code = game_model.verification_code

    if not space_url:
        return False, (
            "No HF Space URL linked to this model. "
            "Add the Space URL and save (Connect Repo) first."
        )
    if not expected_code:
        return False, (
            "No verification code has been issued yet. "
            "Please save the repo first to generate a code."
        )

    subdomain, parse_error = _parse_space_url_subdomain(space_url)
    if parse_error:
        return False, parse_error

    space_repo_id, content, error = _fetch_verify_file_from_space(subdomain)
    if error:
        return False, error

    if content != expected_code:
        return False, (
            f"{VERIFY_FILENAME} in Space '{space_repo_id}' does not match "
            "the expected code. Ensure the file contains exactly the verification "
            "code with no extra whitespace or newlines."
        )

    log.info(
        "Space ownership verified for user=%s game=%s space=%s",
        game_model.user_id, game_model.game_type, space_repo_id,
    )
    return True, f"Space '{space_repo_id}' ownership verified."


def check_data_repo_ownership(game_model: "UserGameModel") -> "tuple[bool, str]":
    """Verify ownership of the linked HF data repo by checking AGL_VERIFY.txt.

    The data repo must contain AGL_VERIFY.txt at its root with content
    identical to game_model.verification_code (the same single code used
    for all three ownership checks).

    Returns ``(success, message)``.  Does NOT update hf_inference_endpoint_status
    directly — that is the responsibility of the caller (``check_full_ownership``).
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
    """Full three-way ownership check: model repo + HF Space + data repo.

    Checks are run in sequence:
      1. Model repo     — AGL_VERIFY.txt == verification_code  (always required)
      2. HF Space repo  — AGL_VERIFY.txt == verification_code  (if Space URL set)
      3. Data repo      — AGL_VERIFY.txt == verification_code  (if data repo set)

    Per-field booleans (model_repo_ownership_verified, space_ownership_verified,
    data_repo_ownership_verified) are reset at the start and then set to True as
    each step passes, so the template can show exactly which repos are verified.

    Any failure immediately sets ``hf_inference_endpoint_status='failed'`` and
    returns ``(False, human-readable error message)``.

    On full success sets ``hf_inference_endpoint_status='ready'`` and returns
    ``(True, summary message)``.
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
        space_ownership_verified=False,
        data_repo_ownership_verified=False,
    )
    game_model.model_repo_ownership_verified = False
    game_model.space_ownership_verified = False
    game_model.data_repo_ownership_verified = False

    # ── 1. Model repo ──────────────────────────────────────────────────
    repo_ok, repo_msg = check_ownership(game_model)
    if not repo_ok:
        _update_space_status(game_model, "failed")
        log.warning(
            "check_full_ownership: model repo FAILED for user=%s game=%s repo=%s",
            game_model.user_id, game_model.game_type, game_model.hf_model_repo_id,
        )
        return False, f"Model repo check failed: {repo_msg}"

    _UGM.objects.filter(pk=game_model.pk).update(model_repo_ownership_verified=True)
    game_model.model_repo_ownership_verified = True

    # ── 2. HF Space (independent — does NOT block model-repo verification) ─
    pending_failures: list[str] = []
    space_url = game_model.hf_inference_endpoint_url
    if space_url:
        space_ok, space_msg = check_space_ownership(game_model)
        if space_ok:
            _UGM.objects.filter(pk=game_model.pk).update(space_ownership_verified=True)
            game_model.space_ownership_verified = True
        else:
            log.warning(
                "check_full_ownership: Space not yet verified for user=%s game=%s space=%s: %s",
                game_model.user_id, game_model.game_type, space_url, space_msg,
            )
            pending_failures.append(f"HF Space: {space_msg}")

    # ── 3. Data repo (independent — does NOT block model-repo verification) ─
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

    # Build a full numbered list in fixed order: model → data repo → space.
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
        n += 1
    if space_url:
        space_verified = game_model.space_ownership_verified
        space_fail_msg = next(
            (f.replace("HF Space: ", "", 1) for f in pending_failures if f.startswith("HF Space:")),
            "not yet verified — add AGL_VERIFY.txt to the Space repo",
        )
        items.append(_row(n, space_verified, "HF Space",
                           space_fail_msg, space_url))

    body = "<br>".join(items)

    if pending_failures:
        # Model repo is verified; space/data still need AGL_VERIFY.txt.
        # Do NOT mark status as "failed" — model ownership is confirmed.
        log.info(
            "check_full_ownership: model repo PASSED but pending items for user=%s game=%s: %s",
            game_model.user_id, game_model.game_type, " | ".join(pending_failures),
        )
        return False, "<strong>Ownership check results:</strong><br>" + body

    # ── All passed ─────────────────────────────────────────────────────
    _update_space_status(game_model, "ready")
    log.info(
        "check_full_ownership: all checks PASSED for user=%s game=%s",
        game_model.user_id, game_model.game_type,
    )
    return True, "<strong>Ownership check results:</strong><br>" + body


def _rollback_verified(game_model: "UserGameModel") -> None:
    """Revert is_verified=False after a partial ownership failure."""
    game_model.is_verified = False
    game_model.save(update_fields=["is_verified"])


def _update_space_status(game_model: "UserGameModel", status: str) -> None:
    """Persist hf_inference_endpoint_status without touching other fields."""
    from apps.users.models import UserGameModel as _UGM
    _UGM.objects.filter(pk=game_model.pk).update(hf_inference_endpoint_status=status)
    game_model.hf_inference_endpoint_status = status


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
