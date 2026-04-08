# ──────────────────────────────────────────────
# apps/users/rating_lock.py
#
# Helpers for the "30 rated games then lock" rule.
#
# Business rules
# ──────────────
# 1. Every user must complete exactly 30 rated games.
# 2. After game 30 the model's commit SHA is frozen
#    (locked_commit_id / locked_at).
# 3. Any model change after locking blocks further
#    rated / tournament play.  Casual games are always
#    allowed.
#
# Security
# ────────
# The HF token is received only as a runtime argument
# and is NEVER stored.  It is used to query the HF Hub
# API and then immediately discarded.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.users.models import CustomUser, UserGameModel

log = logging.getLogger(__name__)

MANDATORY_RATED_GAMES = 30


def _get_game_model(user: CustomUser, game_type: str) -> UserGameModel | None:
    """Return the UserGameModel for *game_type*, or None."""
    from apps.users.models import UserGameModel
    try:
        return UserGameModel.objects.get(user=user, game_type=game_type)
    except UserGameModel.DoesNotExist:
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HF Hub helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_latest_commit_id(repo_id: str, token: str) -> str | None:
    """Return the latest commit SHA for a Hugging Face model repo.

    Uses ``huggingface_hub.list_repo_commits(limit=1)`` which is a
    lightweight API call.  Returns ``None`` on any error so callers
    never need to handle exceptions from the HF SDK.

    The *token* is used only for this single API call and is never
    stored anywhere.
    """
    if not repo_id or not token:
        return None
    try:
        from huggingface_hub import list_repo_commits

        commits = list(list_repo_commits(repo_id, token=token, repo_type="model"))
        if commits:
            return commits[0].commit_id
    except Exception:
        log.debug("Could not fetch latest commit for %s", repo_id, exc_info=True)
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Permission check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def can_play_rated_game(
    user: CustomUser,
    provided_token: str,
    game_type: str = "chess",
) -> tuple[bool, str]:
    """Decide whether *user* is allowed to start a new rated / tournament game.

    Returns ``(allowed, message)``.  When ``allowed`` is ``False`` the
    *message* should be shown to the user.

    Logic
    ─────
    • < 30 rated games → always allow (still in mandatory phase).
    • ≥ 30 rated games → the model must match the locked commit SHA.
      If the user has changed their model, block them with a clear
      explanation.
    """
    gm = _get_game_model(user, game_type)
    repo_id = gm.hf_model_repo_id if gm else user.hf_model_repo_id
    rated = gm.rated_games_played if gm else user.rated_games_played
    locked = gm.locked_commit_id if gm else user.locked_commit_id

    if rated < MANDATORY_RATED_GAMES:
        return True, "ok"

    # Model is supposed to be locked after 30 games.
    current_commit = get_latest_commit_id(repo_id, provided_token)

    if not current_commit:
        return False, (
            "We could not access your model on Hugging Face. "
            "Please double-check your token and try again."
        )

    if locked and current_commit != locked:
        return False, (
            "You have changed your AI agent after completing the "
            f"{MANDATORY_RATED_GAMES} mandatory rated games. "
            "Your original model is now locked for rated and tournament play. "
            "You can still play casual / unrated games with the new version."
        )

    return True, "ok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Model-change detection (for UI warnings)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def has_model_changed(
    user: CustomUser,
    provided_token: str,
    game_type: str = "chess",
) -> bool:
    """Return ``True`` if the user's current model differs from the
    locked version.  Always returns ``False`` before the 30-game mark
    because no lock exists yet.
    """
    gm = _get_game_model(user, game_type)
    repo_id = gm.hf_model_repo_id if gm else user.hf_model_repo_id
    rated = gm.rated_games_played if gm else user.rated_games_played
    locked = gm.locked_commit_id if gm else user.locked_commit_id

    if rated < MANDATORY_RATED_GAMES:
        return False
    if not locked:
        return False
    current = get_latest_commit_id(repo_id, provided_token)
    return current is not None and current != locked


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Post-game locking logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def record_rated_game(
    user: CustomUser,
    provided_token: str | None = None,
    game_type: str = "chess",
) -> None:
    """Increment ``rated_games_played`` and lock the model at game 30.

    Call this exactly once per player after every rated game finishes.

    If the caller provides a *provided_token* and this is the 30th game,
    the current commit SHA will be captured as the locked version.  If no
    token is available at this point (e.g. tournament games run server-side),
    the ``last_known_commit_id`` will be used as fallback.
    """
    gm = _get_game_model(user, game_type)

    # Update on the per-game model if available, otherwise legacy user fields
    if gm:
        gm.rated_games_played += 1

        if gm.rated_games_played == MANDATORY_RATED_GAMES and not gm.locked_commit_id:
            commit = None
            if provided_token:
                commit = get_latest_commit_id(gm.hf_model_repo_id, provided_token)
            if not commit:
                commit = gm.last_known_commit_id
            if commit:
                gm.locked_commit_id = commit
                gm.locked_at = timezone.now()
                log.info(
                    "Model locked for %s/%s at commit %s after %d rated games",
                    user.username, game_type, commit, MANDATORY_RATED_GAMES,
                )

        gm.save(update_fields=[
            "rated_games_played",
            "locked_commit_id",
            "locked_at",
        ])
    else:
        # Legacy fallback for users without a UserGameModel
        user.rated_games_played += 1

        if user.rated_games_played == MANDATORY_RATED_GAMES and not user.locked_commit_id:
            commit = None
            if provided_token:
                commit = get_latest_commit_id(user.hf_model_repo_id, provided_token)
            if not commit:
                commit = user.last_known_commit_id
            if commit:
                user.locked_commit_id = commit
                user.locked_at = timezone.now()
                log.info(
                    "Model locked for %s at commit %s after %d rated games",
                    user.username, commit, MANDATORY_RATED_GAMES,
                )

        user.save(update_fields=[
            "rated_games_played",
            "locked_commit_id",
            "locked_at",
        ])
