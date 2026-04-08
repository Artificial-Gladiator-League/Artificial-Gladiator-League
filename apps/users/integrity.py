# ──────────────────────────────────────────────
# apps/users/integrity.py
#
# Revision-pinned model-integrity verification.
#
# Business rules
# ──────────────
# 1. At registration the user chooses a ref (branch
#    or tag).  We resolve it to an exact full SHA
#    and store that as the approved revision.
# 2. On the first login each day the user supplies
#    their HF read token so we can re-check the ref.
# 3. If the resolved SHA ≠ approved SHA the repo
#    has a new revision available — the user must
#    submit the new revision for approval before
#    resuming rated/tournament play.
#
# Security
# ────────
# The HF token is received only as a runtime
# argument and is NEVER stored.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import mail_admins
from django.utils import timezone

if TYPE_CHECKING:
    from apps.users.models import CustomUser, UserGameModel

log = logging.getLogger(__name__)


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


def run_daily_integrity_check() -> dict:
    """Batch integrity check for all users with models.

    Intended to be called by APScheduler at 20:00 Israel time daily.
    Uses OAuth tokens where available; flags users who need
    manual verification.

    Also runs Docker sandbox re-verification (reverify_model)
    for approved models to detect commit SHA changes.

    Returns a summary dict with counts.
    """
    from apps.users.models import CustomUser, UserGameModel

    today = timezone.now().date()
    stats = {"checked": 0, "passed": 0, "failed": 0, "no_token": 0, "reverified": 0}

    # Find all users who have game models not yet validated today
    user_ids = (
        UserGameModel.objects
        .filter(hf_model_repo_id__gt="")
        .exclude(last_model_validation_date=today)
        .values_list("user_id", flat=True)
        .distinct()
    )
    users = CustomUser.objects.filter(pk__in=user_ids)

    log.info("🔄 ═══ DAILY INTEGRITY CHECK START ═══ (%d users pending)", len(users))

    for user in users:
        stats["checked"] += 1
        # Try OAuth token first
        token = None
        if user.hf_oauth_token_encrypted:
            try:
                from .hf_oauth import get_user_hf_token
                token = get_user_hf_token(user)
            except Exception:
                log.debug("Could not decrypt OAuth token for %s", user.username)

        if not token:
            log.info(
                "⏳ User %s has no stored token — will be prompted on next login",
                user.username,
            )
            stats["no_token"] += 1
            continue

        ok = validate_all_models(user, token)
        if ok:
            stats["passed"] += 1
        else:
            stats["failed"] += 1

    # ── Docker sandbox re-verification for approved models ──
    try:
        from apps.games.local_sandbox_inference import reverify_model

        approved_models = UserGameModel.objects.filter(
            hf_model_repo_id__gt="",
            verification_status="approved",
        ).select_related("user")

        for gm in approved_models:
            try:
                token = None
                if gm.user.hf_oauth_token_encrypted:
                    from .hf_oauth import get_user_hf_token
                    token = get_user_hf_token(gm.user)
                reverify_model(gm, token=token)
                stats["reverified"] += 1
            except Exception:
                log.exception(
                    "Sandbox re-verification failed for %s/%s",
                    gm.user.username, gm.game_type,
                )
    except Exception:
        log.exception("Could not run sandbox re-verification batch")

    log.info(
        "✅ ═══ DAILY INTEGRITY CHECK DONE ═══ "
        "checked=%d passed=%d failed=%d no_token=%d reverified=%d",
        stats["checked"], stats["passed"], stats["failed"],
        stats["no_token"], stats["reverified"],
    )
    return stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tournament eligibility check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REVALIDATION_GAMES_REQUIRED = 30


def can_join_tournament(
    user: CustomUser,
    game_type: str,
) -> tuple[bool, str]:
    """Check if *user* is eligible to join a tournament for *game_type*.

    Performs a **live** HF integrity check on every call (if a token
    is available) so that model changes are caught immediately — even
    if the daily validation already passed earlier today.

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
            "Please re-submit your model for approval before joining "
            "tournaments. You must also complete 30 rated games in "
            f"{game_type} after the change."
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
            result["message"] = (
                f"Tip: your model card is missing these recommended sections: {formatted}. "
                "Adding them helps other players understand your AI agent. "
                "See: " + MODEL_CARD_HELP_URL
            )
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
