# ──────────────────────────────────────────────
# apps/users/integrity.py
#
# Model integrity verification — local-repo edition.
#
# Two integrity modes
# ──────────────────
# 1. LOCAL HASH (primary, post-refactor)
#    Files are git-committed under user_models/user_{id}/{game}/.
#    Integrity = SHA-256 of each model/data file compared to a
#    stored baseline (UserGameModel.local_integrity_baseline).
#    No network I/O.  Run ONLY before tournament entry.
#
# 2. HF REVISION (legacy, kept for backwards compat)
#    Resolves HF branch/tag to a commit SHA.  Used when
#    local_integrity_baseline is empty (old records).
#
# Tournament gate (IMPORTANT)
# ───────────────────────────
# check_local_integrity() is called by the tournament engine
# (tournaments/tasks.py → run_pre_tournament_integrity_checks)
# ONLY, NOT on every login or move.
#
# Security
# ────────
# HF tokens are received only as runtime args, never stored.
# ──────────────────────────────────────────────
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import mail_admins
from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from apps.users.models import CustomUser, UserGameModel

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Local hash-based integrity (primary)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_file_hashes(root: Path) -> dict[str, str]:
    """Return a dict mapping relative file paths → SHA-256 hex digests.

    Walks *root* recursively, skipping:
      - Hidden files/dirs (names starting with ``.``)
      - Internal runner scripts (names starting with ``_agl_``)
      - The ``__pycache__`` directory

    The returned keys are POSIX-style relative paths (forward slashes).
    """
    hashes: dict[str, str] = {}
    if not root or not root.exists():
        return hashes
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" or part.startswith("_agl_") for part in parts):
            continue
        rel = p.relative_to(root).as_posix()
        try:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            hashes[rel] = sha
        except OSError as exc:
            log.warning("Could not hash file %s: %s", p, exc)
    return hashes


def record_local_baseline(game_model: "UserGameModel") -> tuple[bool, str]:
    """Compute SHA-256 hashes of all committed model/data files and persist them.

    Stores the result as JSON in ``UserGameModel.local_integrity_baseline``.
    Call this once at submission/approval time.

    Returns ``(ok, message)``.
    """
    from apps.games.local_inference import resolve_model_path

    user_id = game_model.user_id
    game_type = game_model.game_type
    model_dir, data_dir = resolve_model_path(user_id, game_type)

    if model_dir is None:
        return False, f"No local model files found for user={user_id} game={game_type}"

    hashes: dict[str, str] = {}
    # Hash model files with prefix "model/"
    for rel, sha in compute_file_hashes(model_dir).items():
        hashes[f"model/{rel}"] = sha
    # Hash data files with prefix "data/"
    if data_dir:
        for rel, sha in compute_file_hashes(data_dir).items():
            hashes[f"data/{rel}"] = sha

    if not hashes:
        return False, "No files to hash — model directory appears empty"

    baseline_json = json.dumps(hashes, sort_keys=True)
    try:
        game_model.local_integrity_baseline = baseline_json
        update_fields = ["local_integrity_baseline"]
        # Also record the git commit SHA if available (best-effort)
        git_sha = _get_git_commit_sha()
        if git_sha and hasattr(game_model, "original_model_commit_sha"):
            game_model.original_model_commit_sha = git_sha
            update_fields.append("original_model_commit_sha")
        game_model.save(update_fields=update_fields)
        log.info(
            "Recorded local integrity baseline for user=%s game=%s (%d files)",
            user_id, game_type, len(hashes),
        )
    except Exception as exc:
        log.exception("Failed to save integrity baseline for user=%s game=%s", user_id, game_type)
        return False, f"DB save failed: {exc}"

    return True, f"Baseline recorded ({len(hashes)} files)"


def check_local_integrity(
    game_model: "UserGameModel",
    *,
    alert_admins: bool = True,
) -> tuple[bool, str]:
    """Compare current file hashes against the stored baseline.

    Called ONLY before tournament entry (not on every login/move).

    Returns ``(ok, message)``.
    If files changed, sets ``model_integrity_ok = False`` on the record
    and optionally emails admins.
    """
    from apps.games.local_inference import resolve_model_path

    user_id = game_model.user_id
    game_type = game_model.game_type
    baseline_json = getattr(game_model, "local_integrity_baseline", "") or ""

    # No baseline yet → record it now (first tournament for this model).
    if not baseline_json:
        log.info(
            "No local integrity baseline for user=%s game=%s — recording now",
            user_id, game_type,
        )
        ok, msg = record_local_baseline(game_model)
        if not ok:
            return False, f"Could not record baseline: {msg}"
        # Baseline just recorded — this is the approved state.
        return True, "Baseline established; integrity OK"

    try:
        baseline: dict[str, str] = json.loads(baseline_json)
    except json.JSONDecodeError:
        log.error("Corrupt integrity baseline for user=%s game=%s", user_id, game_type)
        return False, "Integrity baseline is corrupt — please re-submit your model"

    model_dir, data_dir = resolve_model_path(user_id, game_type)
    if model_dir is None:
        _mark_integrity_failed(game_model, "model files missing")
        return False, f"Model files missing for user={user_id} game={game_type}"

    # Compute current hashes
    current: dict[str, str] = {}
    for rel, sha in compute_file_hashes(model_dir).items():
        current[f"model/{rel}"] = sha
    if data_dir:
        for rel, sha in compute_file_hashes(data_dir).items():
            current[f"data/{rel}"] = sha

    # Compare
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    changed = sorted(k for k in (set(current) & set(baseline)) if current[k] != baseline[k])

    if not added and not removed and not changed:
        # Update last-checked date
        try:
            game_model.last_model_validation_date = timezone.now().date()
            game_model.model_integrity_ok = True
            game_model.save(update_fields=["last_model_validation_date", "model_integrity_ok"])
        except Exception:
            pass
        return True, "Integrity OK — all hashes match"

    diff_summary = []
    if added:
        diff_summary.append(f"added: {added}")
    if removed:
        diff_summary.append(f"removed: {removed}")
    if changed:
        diff_summary.append(f"changed: {changed}")
    msg = "Model files changed since baseline — " + "; ".join(diff_summary)

    log.warning(
        "Integrity check FAILED for user=%s game=%s: %s",
        user_id, game_type, msg,
    )
    _mark_integrity_failed(game_model, msg)

    if alert_admins:
        try:
            mail_admins(
                subject=f"[AGL] Integrity FAIL — user={user_id} game={game_type}",
                message=(
                    f"User {user_id} game={game_type} failed pre-tournament integrity check.\n\n"
                    f"Details: {msg}\n\n"
                    f"Repo: {getattr(game_model, 'hf_model_repo_id', '?')}"
                ),
                fail_silently=True,
            )
        except Exception:
            pass

    return False, msg


def _mark_integrity_failed(game_model: "UserGameModel", reason: str) -> None:
    """Persist model_integrity_ok=False on a UserGameModel."""
    try:
        game_model.model_integrity_ok = False
        game_model.save(update_fields=["model_integrity_ok"])
    except Exception:
        log.exception("Failed to mark integrity_ok=False for user=%s", getattr(game_model, "user_id", "?"))


def _get_git_commit_sha() -> str | None:
    """Return the current Git HEAD commit SHA of the repo (best-effort).

    Used to version model file snapshots. Returns None if git is
    unavailable or the directory is not a Git repo.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(getattr(settings, "BASE_DIR", ".")),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _resolve_ref_sha(
    repo_id: str,
    token: str,
    ref: str = "main",
    repo_type: str = "model",
) -> str | None:
    """Resolve a branch or tag name to a full commit SHA.

    Steps:
    1. ``auth_check()`` verifies the token can access the repo.
    2. ``list_repo_refs()`` retrieves branch/tag refs — we match
       *ref* against branch names first, then tag names.

    Returns None on any error so callers never need to catch SDK exceptions.
    """
    if not repo_id or not token:
        return None
    try:
        from huggingface_hub import auth_check, list_repo_refs

        auth_check(repo_id, token=token, repo_type=repo_type)

        refs = list_repo_refs(repo_id, token=token, repo_type=repo_type)

        # Try branches first
        for branch in refs.branches:
            if branch.name == ref:
                return branch.target_commit
        # Then tags
        for tag in refs.tags:
            if tag.name == ref:
                return tag.target_commit
        # Fallback: first branch available
        if refs.branches:
            return refs.branches[0].target_commit
    except Exception:
        log.debug("Could not resolve ref '%s' for %s", ref, repo_id, exc_info=True)
    return None


# Keep the old name as an alias so callers that haven't been
# updated yet continue to work.
_get_current_commit_sha = _resolve_ref_sha


def record_original_sha(
    game_model: UserGameModel,
    hf_token: str,
    ref: str = "main",
    repo_type: str = "model",
) -> None:
    """Pin the model's approved revision at submission time.

    Resolves *ref* to an exact SHA and stores the full submission
    identity on the UserGameModel record.
    """
    repo_id = game_model.hf_model_repo_id
    sha = _resolve_ref_sha(repo_id, hf_token, ref=ref, repo_type=repo_type)
    if sha:
        now = timezone.now()
        game_model.original_model_commit_sha = sha
        game_model.last_known_commit_id = sha
        game_model.last_model_validation_date = now.date()
        game_model.model_integrity_ok = True
        game_model.submission_repo_type = repo_type
        game_model.submitted_ref = ref
        game_model.approved_full_sha = sha
        game_model.pinned_at = now
        game_model.rated_games_since_revalidation = 0
        game_model.save(update_fields=[
            "original_model_commit_sha",
            "last_known_commit_id",
            "last_model_validation_date",
            "model_integrity_ok",
            "submission_repo_type",
            "submitted_ref",
            "approved_full_sha",
            "pinned_at",
            "rated_games_since_revalidation",
        ])
        log.info("Pinned approved revision for %s/%s: %s@%s → %s",
                 game_model.user.username, game_model.game_type,
                 repo_id, ref, sha[:12])


def validate_model_integrity(
    game_model: UserGameModel, fresh_token: str
) -> tuple[bool, str]:
    """Check whether a UserGameModel's pinned ref still resolves to the approved SHA.

    Returns ``(success, message)``.
    """
    repo_id = game_model.hf_model_repo_id
    if not repo_id:
        return False, "No Hugging Face model is linked for this game."

    ref = game_model.submitted_ref or "main"
    repo_type = game_model.submission_repo_type or "model"
    pinned_sha = game_model.approved_full_sha or game_model.original_model_commit_sha

    current_sha = _resolve_ref_sha(repo_id, fresh_token, ref=ref, repo_type=repo_type)

    if current_sha is None:
        return False, (
            "We could not verify your model on Hugging Face. "
            "Please check that your token is valid and try again."
        )

    # First successful check — no revision pinned yet
    if not pinned_sha:
        # Preserve any rated-game progress accumulated before the auto-pin.
        # record_original_sha resets the counter to 0, which is correct for
        # brand-new submissions but wrong when the SHA was simply not captured
        # at registration time (e.g. HF API was temporarily unreachable).
        saved_counter = game_model.rated_games_since_revalidation
        record_original_sha(game_model, fresh_token, ref=ref, repo_type=repo_type)
        if saved_counter > 0:
            game_model.rated_games_since_revalidation = saved_counter
            game_model.save(update_fields=["rated_games_since_revalidation"])
        log.info("Auto-pinned revision for %s/%s (missed at submission): %s",
                 game_model.user.username, game_model.game_type, current_sha[:12])
        return True, "Model verified and pinned successfully. You're cleared for today."

    # Revision changed → new revision available
    # Block tournaments (model_integrity_ok = False) but let the user
    # into the site — daily validation still passes so the middleware
    # does not loop-redirect.
    if current_sha != pinned_sha:
        game_model.model_integrity_ok = False
        game_model.last_known_commit_id = current_sha
        game_model.last_model_validation_date = timezone.now().date()
        game_model.rated_games_since_revalidation = 0
        game_model.save(update_fields=[
            "model_integrity_ok",
            "last_known_commit_id",
            "last_model_validation_date",
            "rated_games_since_revalidation",
        ])

        mail_admins(
            subject=f"[AGL] New revision detected: {game_model.user.username}/{game_model.game_type}",
            message=(
                f"User: {game_model.user.username} (pk={game_model.user.pk})\n"
                f"Game: {game_model.game_type}\n"
                f"Repo: {repo_id}\n"
                f"Ref:  {ref}\n"
                f"Pinned SHA:  {pinned_sha}\n"
                f"Current SHA: {current_sha}\n\n"
                "A new revision is available. The user must submit it "
                "for approval before resuming tournament play."
            ),
        )
        log.warning(
            "New revision for %s/%s: pinned %s, current %s — "
            "tournaments blocked, games allowed",
            game_model.user.username, game_model.game_type,
            pinned_sha[:12], current_sha[:12],
        )
        return True, (
            f"A new revision has been detected on '{ref}' for your model. "
            "Your pinned revision no longer matches the current ref. "
            "Tournament entry is blocked until you submit the new revision "
            "for approval. Casual and rated games are still available."
        )

    # Pinned revision matches → all clear
    was_failed = not game_model.model_integrity_ok
    game_model.model_integrity_ok = True
    game_model.last_known_commit_id = current_sha
    game_model.last_model_validation_date = timezone.now().date()
    update_fields = [
        "model_integrity_ok",
        "last_known_commit_id",
        "last_model_validation_date",
    ]
    if was_failed:
        # Re-validation after a revision change — reset the 30-game counter
        game_model.rated_games_since_revalidation = 0
        update_fields.append("rated_games_since_revalidation")
    game_model.save(update_fields=update_fields)
    log.info("Revision OK for %s/%s (%s@%s = %s)",
             game_model.user.username, game_model.game_type,
             repo_id, ref, current_sha[:12])
    return True, "Model verified successfully. You're cleared for today."


def needs_daily_validation(user: CustomUser, game_type: str = None) -> bool:
    """Return True if any (or a specific) game model hasn't been validated today.

    Also returns True for legacy users who have ``hf_model_repo_id`` set
    on the CustomUser model but no UserGameModel entries yet.
    """
    from apps.users.models import UserGameModel

    today = timezone.now().date()
    qs = UserGameModel.objects.filter(user=user, hf_model_repo_id__gt="")
    if game_type:
        qs = qs.filter(game_type=game_type)

    if qs.exists():
        return qs.exclude(last_model_validation_date=today).exists()

    # Legacy fallback: user has hf_model_repo_id on CustomUser but no
    # UserGameModel entries — check the legacy field on CustomUser.
    if user.hf_model_repo_id:
        return user.last_model_validation_date != today

    return False


def validate_all_models(user: CustomUser, hf_token: str) -> bool:
    """Validate all of a user's game models in one pass.

    Returns True only if every model passes integrity check.
    """
    from apps.users.models import UserGameModel

    game_models = UserGameModel.objects.filter(user=user, hf_model_repo_id__gt="")
    if not game_models.exists():
        # Legacy user — no UserGameModel entries
        log.info("No UserGameModel entries for user=%s, skipping validation", user.username)
        return True

    all_ok = True
    for gm in game_models:
        success, msg = validate_model_integrity(gm, hf_token)
        log.info(
            "🔍 Validated %s/%s: %s — %s",
            user.username, gm.game_type,
            "✅ OK" if success else "❌ FAILED", msg,
        )
        if not success:
            all_ok = False
        elif not gm.model_integrity_ok:
            # Validation reached HF successfully but the model's
            # pinned revision no longer matches — treat as failure.
            all_ok = False
    return all_ok


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tournament eligibility check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REVALIDATION_GAMES_REQUIRED = 30


def can_join_tournament(
    user: CustomUser,
    game_type: str,
    *,
    tournament_type: str | None = None,
) -> tuple[bool, str]:
    """Check if *user* is eligible to join a tournament for *game_type*.

    Performs a **live** HF integrity check on every call (if a token
    is available) so that model changes are caught immediately — even
    if the daily validation already passed earlier today.

    QA tournaments bypass the model-integrity flag and the 30-rated-
    games revalidation gate so testers can exercise the anti-cheat
    pipeline without first grinding through 30 ranked games.

    Returns ``(allowed, reason)``.
    """
    from apps.users.models import UserGameModel

    gm = UserGameModel.objects.filter(
        user=user, game_type=game_type,
    ).first()

    if not gm:
        return False, (
            f"No {game_type} model registered. "
            "Upload a model before joining this tournament."
        )

    if not gm.hf_model_repo_id:
        return False, (
            f"No Hugging Face repo linked for {game_type}. "
            "Connect a model repo first."
        )

    # ── QA tournaments: skip integrity + revalidation gates ──
    # QA exists specifically to test the anti-cheat audit pipeline,
    # so a tester whose model_integrity_ok is False or who has not
    # yet completed 30 rated games is still allowed in.
    if tournament_type == "qa":
        return True, "OK"

    # ── Live integrity check on every join attempt ──
    # Always verify the model's current HF SHA matches the approved
    # revision, so last-minute model changes are caught immediately.
    token = _get_stored_token(user)
    if token:
        validate_model_integrity(gm, token)
        gm.refresh_from_db()
    else:
        # No token available — block if model was previously flagged
        # or has never been validated.
        if not gm.model_integrity_ok:
            return False, (
                "Your model integrity is unverified and we have no "
                "stored HF token to check. Please log in with your "
                "Hugging Face account to re-validate."
            )

    if not gm.model_integrity_ok:
        return False, (
            "Your AI model has been modified since it was approved. "
            f"You must complete 30 rated games in {game_type} before "
            f"registering to a {game_type} tournament."
        )

    if gm.rated_games_since_revalidation < REVALIDATION_GAMES_REQUIRED:
        played = gm.rated_games_since_revalidation
        remaining = REVALIDATION_GAMES_REQUIRED - played
        # Distinguish new users from users who changed their repo:
        # if locked_commit_id was never set the user has never
        # completed a 30-game cycle before → brand-new registration.
        if not gm.locked_commit_id:
            return False, (
                f"Welcome! As a new player you must complete "
                f"{REVALIDATION_GAMES_REQUIRED} rated {game_type} games "
                f"before you can join tournaments. "
                f"You have played {played} so far — {remaining} to go."
            )
        return False, (
            f"A change was detected in your model repo. "
            f"You must play {REVALIDATION_GAMES_REQUIRED} rated "
            f"{game_type} games before you can join tournaments again. "
            f"You have played {played} so far — {remaining} to go."
        )

    return True, "OK"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Strict eligibility gate (Rule 1 + Rule 2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOURNAMENT_MIN_RATED_GAMES = 30


def is_eligible_for_tournament(
    user: CustomUser,
    game_type: str,
) -> tuple[bool, str]:
    """Strict tournament eligibility gate.

    Enforces two rules:

    Rule 1 — Experience: the user must have completed at least
    ``TOURNAMENT_MIN_RATED_GAMES`` rated games in *game_type*.

    Rule 2 — Model stability: the user's HF model repo must NOT have
    been modified after the timestamp of their (current count - 30)th
    rated game. In other words: no commits to the repo within the
    span of their last 30 rated games.

    Returns ``(allowed, reason)`` — *reason* is a human-readable
    explanation suitable for showing in a UI message.
    """
    from apps.users.models import UserGameModel
    from apps.games.models import Game

    # ── Rule 1: Experience gate ────────────────────────────
    rated_qs = (
        Game.objects
        .filter(game_type=game_type)
        .filter(models.Q(white=user) | models.Q(black=user))
        .exclude(result__in=(Game.Result.NONE, ""))
        .exclude(is_tournament_game=True)  # tournament games don't count toward qualification
        .order_by("-timestamp")
    )
    rated_count = rated_qs.count()

    if rated_count < TOURNAMENT_MIN_RATED_GAMES:
        remaining = TOURNAMENT_MIN_RATED_GAMES - rated_count
        return False, (
            f"You need {remaining} more rated {game_type} game"
            f"{'s' if remaining != 1 else ''} to qualify for tournaments "
            f"({rated_count} / {TOURNAMENT_MIN_RATED_GAMES} played)."
        )

    # ── Rule 2: Model stability gate ───────────────────────
    gm = UserGameModel.objects.filter(
        user=user, game_type=game_type,
    ).first()
    if not gm or not gm.hf_model_repo_id:
        return False, (
            f"No {game_type} model repo registered. "
            "Connect your Hugging Face repo first."
        )

    # Timestamp of the 30-games-ago boundary game (the oldest of the
    # most recent 30 rated games)
    boundary_game = rated_qs[TOURNAMENT_MIN_RATED_GAMES - 1]
    boundary_ts = boundary_game.timestamp

    # Latest commit timestamp on the HF repo
    latest_commit_ts = _get_repo_latest_commit_time(gm.hf_model_repo_id)
    if latest_commit_ts is None:
        # Could not reach HF — fail closed for tournaments
        return False, (
            "We could not reach Hugging Face to verify your repo's commit "
            "history. Please try again in a moment."
        )

    # Normalize timezones for comparison
    import django.utils.timezone as dj_tz
    if dj_tz.is_naive(latest_commit_ts):
        latest_commit_ts = dj_tz.make_aware(latest_commit_ts)
    if dj_tz.is_naive(boundary_ts):
        boundary_ts = dj_tz.make_aware(boundary_ts)

    if latest_commit_ts > boundary_ts:
        # Repo was modified inside the last-30-games window.
        # Count how many of the user's recent rated games were played
        # AFTER the most recent commit — these "count toward" the
        # required cooldown of 30 post-commit games.
        games_since_commit = rated_qs.filter(timestamp__gt=latest_commit_ts).count()
        remaining = TOURNAMENT_MIN_RATED_GAMES - games_since_commit
        return False, (
            f"Your model repository was updated on "
            f"{latest_commit_ts.strftime('%Y-%m-%d %H:%M UTC')} — too recently. "
            f"Please play {remaining} more rated {game_type} game"
            f"{'s' if remaining != 1 else ''} before joining a tournament "
            f"({games_since_commit} / {TOURNAMENT_MIN_RATED_GAMES} since the update)."
        )

    return True, "OK"


def _get_repo_latest_commit_time(repo_id: str):
    """Return the datetime of the latest commit on *repo_id*, or None on error.

    Uses the platform HF token so gated repos that have granted access to
    the platform account are reachable.
    """
    try:
        from huggingface_hub import list_repo_commits
        token = getattr(settings, "HF_PLATFORM_TOKEN", None) or None
        commits = list(list_repo_commits(repo_id, repo_type="model", token=token))
        if commits:
            return commits[0].created_at
    except Exception as exc:
        log.warning(
            "_get_repo_latest_commit_time: failed for %s: %s", repo_id, exc,
        )
    return None


def _get_stored_token(user: CustomUser) -> str | None:
    """Retrieve the best available HF token for *user*.

    Tries the user's own OAuth token first, then falls back to the
    platform-level ``HF_PLATFORM_TOKEN`` so we can still verify
    public / platform-accessible repos for non-OAuth users.
    """
    if user.hf_oauth_token_encrypted:
        try:
            from .hf_oauth import get_user_hf_token
            token = get_user_hf_token(user)
            if token:
                return token
        except Exception:
            log.debug("Could not decrypt HF token for %s", user.username, exc_info=True)
    # Fallback: platform token
    return getattr(settings, "HF_PLATFORM_TOKEN", None) or None


def check_participants_integrity(
    participants,
    game_type: str,
) -> list[tuple[str, str]]:
    """Live-check integrity for a list of tournament participants.

    For each participant, runs a fresh HF SHA check (if token available
    and model not validated today) so last-minute model changes are caught
    right before the tournament starts.

    Returns a list of ``(username, reason)`` for participants who fail.
    """
    from apps.users.models import UserGameModel

    user_ids = [p.user_id for p in participants]
    gm_map = {
        gm.user_id: gm
        for gm in UserGameModel.objects.filter(
            user_id__in=user_ids, game_type=game_type,
        ).select_related("user")
    }

    failures: list[tuple[str, str]] = []

    for p in participants:
        gm = gm_map.get(p.user_id)

        if not gm:
            failures.append((p.user.username, f"no {game_type} model registered"))
            continue

        # Live check — run validation if not done today
        if needs_daily_validation(p.user, game_type):
            token = _get_stored_token(p.user)
            if token:
                validate_model_integrity(gm, token)
                gm.refresh_from_db()

        if not gm.model_integrity_ok:
            failures.append((
                p.user.username,
                f"{game_type} model modified — failed integrity check",
            ))
        elif gm.rated_games_since_revalidation < REVALIDATION_GAMES_REQUIRED:
            remaining = REVALIDATION_GAMES_REQUIRED - gm.rated_games_since_revalidation
            failures.append((
                p.user.username,
                f"only {gm.rated_games_since_revalidation}/{REVALIDATION_GAMES_REQUIRED} "
                f"post-change rated {game_type} games ({remaining} remaining)",
            ))

    return failures


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Model card check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Required sections that every official submission should document.
MODEL_CARD_EXPECTED_SECTIONS = [
    "game supported",
    "interface version",
    "intended use",
    "limitations",
    "training method",
    "release notes",
]

MODEL_CARD_HELP_URL = (
    "https://huggingface.co/docs/hub/model-cards"
)


def check_model_card(repo_id: str, token: str) -> dict:
    """Check whether a HF repo has a model card with the expected sections.

    Returns a dict with:
        ``has_card``   — True if the repo has any README / model card.
        ``card_text``  — The raw model card content (empty string if missing).
        ``missing``    — List of expected section headings not found.
        ``message``    — A user-friendly summary.
    """
    result = {
        "has_card": False,
        "card_text": "",
        "missing": list(MODEL_CARD_EXPECTED_SECTIONS),
        "message": "",
    }
    log.info("[model-card] ===== START check_model_card(repo='%s') =====", repo_id)
    if not repo_id:
        log.info("[model-card] repo_id is empty — skipping check")
        result["message"] = "No repository specified."
        return result

    try:
        from huggingface_hub import ModelCard
        from huggingface_hub.utils import EntryNotFoundError

        log.info("[model-card] Using ModelCard.load() to fetch README from '%s'", repo_id)

        try:
            card = ModelCard.load(repo_id, token=token or None)
            card_text = card.content or ""
            log.info("[model-card] ModelCard.load() succeeded, content length=%d", len(card_text))
        except EntryNotFoundError:
            card_text = ""
            log.info("[model-card] EntryNotFoundError — no README.md in repo")

        if not card_text.strip():
            log.info("[model-card] RESULT: NO model card found for repo=%s", repo_id)
            result["message"] = (
                "Your model repository does not have a model card (README.md). "
                "A model card helps other players understand your AI agent. "
                "Please add one \u2014 see: " + MODEL_CARD_HELP_URL
            )
            return result

        result["has_card"] = True
        result["card_text"] = card_text
        log.info("[model-card] RESULT: model card FOUND (%d chars), first 200: %.200s",
                 len(card_text), card_text)

        # Check which expected sections are present.
        # Accept: markdown headings (## Game Supported), plain text,
        # underscores/hyphens instead of spaces, any casing.
        import re
        lower_text = card_text.lower()
        missing = []
        for section in MODEL_CARD_EXPECTED_SECTIONS:
            # Build a flexible pattern: allow _ or - or space between words
            words = section.split()
            flex = r"[\s_\-]".join(re.escape(w) for w in words)
            if not re.search(flex, lower_text):
                missing.append(section)
        result["missing"] = missing

        if result["missing"]:
            formatted = ", ".join(f'"{s.title()}"' for s in result["missing"])
            log.info("[model-card] Missing sections (warning only): %s", formatted)
            # Per user request: remove the user-facing tip about missing sections.
            result["message"] = ""
        else:
            log.info("[model-card] All recommended sections present!")
            result["message"] = "Model card looks great \u2014 all recommended sections found."

    except Exception:
        log.warning("[model-card] EXCEPTION fetching model card for %s", repo_id, exc_info=True)
        result["message"] = (
            "Could not check your model card right now. "
            "Please make sure your repository has a README.md. "
            "See: " + MODEL_CARD_HELP_URL
        )

    log.info("[model-card] ===== END check_model_card — has_card=%s, missing=%s =====",
             result["has_card"], result["missing"])
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tournament participant SHA integrity checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_participant_sha(participant) -> tuple[bool, str]:
    """Check whether a tournament participant's model still matches their
    registered SHA.

    Uses the read-only token stored on the participant at registration time.
    Returns ``(ok, reason)``.

    Returns ``(True, ...)`` if the check is skipped (no token / no SHA /
    cannot reach HF) so that a temporary network outage never causes an
    innocent player to be disqualified.
    """
    token = (participant.tournament_hf_token or "").strip()
    registered_sha = (participant.registered_sha or "").strip()

    if not token or not registered_sha:
        log.debug(
            "SHA check skipped for participant %s (no token or no registered_sha)",
            participant.user_id,
        )
        return True, "No token / SHA stored — check skipped"

    from apps.users.models import UserGameModel

    try:
        tournament = participant.tournament
        game_type = tournament.game_type
    except Exception:
        return True, "Could not resolve tournament game type — check skipped"

    gm = UserGameModel.objects.filter(
        user_id=participant.user_id,
        game_type=game_type,
    ).first()

    if not gm:
        return True, f"No {game_type} game model found — check skipped"

    repo_id = gm.hf_model_repo_id or ""
    if not repo_id:
        return True, "No model repo linked — check skipped"

    ref = gm.submitted_ref or "main"
    repo_type = gm.submission_repo_type or "model"

    current_sha = _resolve_ref_sha(repo_id, token, ref=ref, repo_type=repo_type)

    if current_sha is None:
        # Could not reach HF — do NOT disqualify; log and skip.
        log.warning(
            "Could not resolve SHA for participant user=%s repo=%s — check skipped",
            participant.user_id, repo_id,
        )
        return True, "Could not contact HF — check skipped (network error)"

    if current_sha != registered_sha:
        log.warning(
            "SHA mismatch for participant user=%s tournament=%s: "
            "registered=%s current=%s",
            participant.user_id,
            getattr(tournament, "pk", "?"),
            registered_sha[:12],
            current_sha[:12],
        )
        return False, (
            f"Your model repository was modified after you registered for this tournament. "
            f"SHA at registration: {registered_sha[:12]}…, current SHA: {current_sha[:12]}…. "
            "Immediate disqualification applied per the Fair Play Policy."
        )

    return True, "SHA matches — integrity OK"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unified live HF SHA check (join / pre-round / mid-game)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def live_sha_check(
    game_model: "UserGameModel",
    *,
    context: str = "join",
    fail_open: bool = True,
) -> tuple[bool, str | None, str | None]:
    """Compare the approved DB SHA against the latest HF commit SHA.

    Used at three points:
      • Tournament Join button click          (context="join")
      • Pre-tournament round start            (context="pre-round")
      • Mid-game random spot-check            (context="mid-game")

    Always emits a standardized line via ``print(...)`` so the HF API
    request is visible in the Django runserver console regardless of
    LOGGING configuration:

        HF API check for repo {repo}: current_sha={db_sha}, latest={hf_sha}

    Returns ``(matches, db_sha, latest_sha)``.

    On mismatch:
      • ``model_integrity_ok`` is set to False on the game model.
      • ``last_known_commit_id`` is updated to the new HF sha.
      • ``rated_games_since_revalidation`` is reset to 0 so the user
        must replay the cooldown.
      • Admins are emailed (best-effort).

    On HF unreachable:
      • If *fail_open* is True (default) returns ``(True, db_sha, None)``
        so a transient network outage does not punish honest users.
      • If *fail_open* is False returns ``(False, db_sha, None)`` so the
        caller can block (used at the strictest gates).
    """
    from apps.users.models import UserGameModel  # noqa: F401 — type hint only

    repo_id = (game_model.hf_model_repo_id or "").strip()
    db_sha = (
        game_model.approved_full_sha
        or game_model.original_model_commit_sha
        or game_model.last_known_commit_id
        or ""
    ).strip() or None

    if not repo_id:
        print(f"HF API check for repo <none>: current_sha={db_sha}, latest=<skipped>")
        return True, db_sha, None

    ref = (game_model.submitted_ref or "main").strip() or "main"
    repo_type = (game_model.submission_repo_type or "model").strip() or "model"

    token = _get_stored_token(game_model.user)
    latest_sha: str | None = None
    try:
        latest_sha = _resolve_ref_sha(repo_id, token or "", ref=ref, repo_type=repo_type)
    except Exception:
        log.warning(
            "live_sha_check: HF call raised for repo=%s (%s) — treating as unreachable",
            repo_id, context, exc_info=True,
        )
        latest_sha = None

    # Standardised, always-on log line — visible even if logging is muted.
    print(
        f"HF API check for repo {repo_id}: current_sha={db_sha}, latest={latest_sha}"
    )
    log.info(
        "live_sha_check[%s] user=%s game=%s repo=%s db_sha=%s latest=%s",
        context,
        getattr(game_model.user, "username", "?"),
        game_model.game_type,
        repo_id,
        db_sha,
        latest_sha,
    )

    # Network failure — caller decides whether to fail open or closed.
    if latest_sha is None:
        return (fail_open, db_sha, None)

    # No baseline yet — don't punish, but record what we just saw.
    if not db_sha:
        try:
            game_model.last_known_commit_id = latest_sha
            game_model.last_model_validation_date = timezone.now().date()
            game_model.save(update_fields=[
                "last_known_commit_id", "last_model_validation_date",
            ])
        except Exception:
            log.exception("live_sha_check: failed to persist initial sha for %s", repo_id)
        return True, latest_sha, latest_sha

    if latest_sha == db_sha:
        # Refresh validation date — cheap optimisation for daily checks.
        try:
            game_model.last_model_validation_date = timezone.now().date()
            game_model.last_known_commit_id = latest_sha
            game_model.save(update_fields=[
                "last_model_validation_date", "last_known_commit_id",
            ])
        except Exception:
            pass
        return True, db_sha, latest_sha

    # ── Mismatch ──────────────────────────────────────────
    log.warning(
        "SHA MISMATCH detected (%s) for user=%s game=%s repo=%s: "
        "db=%s vs hf=%s",
        context,
        getattr(game_model.user, "username", "?"),
        game_model.game_type,
        repo_id,
        db_sha[:12],
        latest_sha[:12],
    )
    try:
        game_model.model_integrity_ok = False
        game_model.last_known_commit_id = latest_sha
        game_model.last_model_validation_date = timezone.now().date()
        game_model.rated_games_since_revalidation = 0
        game_model.save(update_fields=[
            "model_integrity_ok",
            "last_known_commit_id",
            "last_model_validation_date",
            "rated_games_since_revalidation",
        ])
    except Exception:
        log.exception(
            "live_sha_check: failed to persist mismatch state for user=%s repo=%s",
            getattr(game_model.user, "username", "?"), repo_id,
        )

    try:
        mail_admins(
            subject=(
                f"[AGL] Repo SHA changed ({context}): "
                f"{getattr(game_model.user, 'username', '?')}/{game_model.game_type}"
            ),
            message=(
                f"Context: {context}\n"
                f"User:    {getattr(game_model.user, 'username', '?')} "
                f"(pk={getattr(game_model.user, 'pk', '?')})\n"
                f"Game:    {game_model.game_type}\n"
                f"Repo:    {repo_id}\n"
                f"DB SHA:  {db_sha}\n"
                f"HF SHA:  {latest_sha}\n"
            ),
            fail_silently=True,
        )
    except Exception:
        pass

    return False, db_sha, latest_sha
