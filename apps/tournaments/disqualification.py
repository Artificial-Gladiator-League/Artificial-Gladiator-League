"""Disqualification service for the anti-cheat SHA pipeline.

Single source of truth for "the user's repo changed during a live
tournament". Centralising this here keeps the behaviour identical
whether the disqualification was triggered by:

  * the per-round guaranteed integrity check,
  * the every-30s probabilistic Celery audit,
  * an admin manual action, or
  * the join-time gate (rare — usually blocked earlier).

Behaviour
---------
* **All tournament types** (SMALL / LARGE / GAUNTLET / QA):
    - ``disqualified_for_sha_mismatch = True``
    - ``eliminated = True`` so the bracket / standings stop showing
      the player as "still in".
    - ``ready = False`` so the offender is unregistered from the
      active/registered roster — the lobby views filter on these
      fields, so the player disappears from the lineup immediately.
    - Live match (if any) is forfeited via the engine pipeline.

  We deliberately do NOT delete the row — the audit trail
  (``disqualified_for_sha_mismatch=True`` plus the
  ``TournamentShaCheck`` row) must survive for forensics, and the
  kept row also blocks re-registration into the same tournament.

* **Regular tournaments** additionally keep the offender out of
  future ranked tournaments via the per-user integrity cooldown
  (``repo_changed=True`` / 30-rated-games reset, applied below). QA
  tournaments skip that ranked gate by design so the anti-cheat flow
  stays testable.

In both cases:
    - A loud terminal banner is printed.
    - The whole state change is wrapped in ``transaction.atomic()``
      so a partial failure cannot leave the participant half-DQ'd.
    - The current request session (if any) is *not* touched here —
      the ``DisqualificationInterceptMiddleware`` reads the participant
      flag directly on every request, so the redirect-to-/tournaments/
      disqualified/ page kicks in on the very next page load.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.db import transaction

log = logging.getLogger(__name__)


def disqualify_for_repo_change(
    participant,
    *,
    reason: str,
    forfeit_live_match: bool = True,
    new_sha: Optional[str] = None,
) -> dict:
    """Atomically disqualify *participant* for an SHA mismatch.

    Idempotent: calling twice on the same participant is a no-op.
    Returns a small report dict useful for tests + admin actions.

    Parameters
    ----------
    participant : TournamentParticipant
        The row to disqualify. ``participant.tournament`` must be
        loaded (it is — the audit pipeline always select_related's it).
    reason : str
        Human-readable reason; printed to the terminal and persisted
        on the participant for the admin UI.
    forfeit_live_match : bool, default True
        When True, calls ``engine._handle_mid_round_disqualification``
        so the user's currently-live game (if any) is forfeited and
        the opponent gets a walkover. Set False in unit tests that
        only want to verify the flag flip.
    """
    from apps.tournaments.models import Tournament

    tournament = participant.tournament
    is_qa = tournament.type == Tournament.Type.QA.value or (
        tournament.type == Tournament.Type.QA
    )

    report = {
        "participant_id": participant.pk,
        "user_id": participant.user_id,
        "username": participant.user.username,
        "tournament_id": tournament.pk,
        "tournament_type": str(tournament.type),
        "is_qa": is_qa,
        "already_disqualified": False,
        "forfeit_attempted": False,
        "forfeit_ok": False,
        "removed_from_qa_lobby": False,
    }

    # ── Idempotency guard ───────────────────────────────────────
    if participant.disqualified_for_sha_mismatch:
        report["already_disqualified"] = True
        return report

    # ── Atomic state flip ───────────────────────────────────────
    with transaction.atomic():
        # Re-fetch inside the transaction with select_for_update so
        # two concurrent audit dispatches can't double-process.
        try:
            from apps.tournaments.models import TournamentParticipant
            locked = (
                TournamentParticipant.objects
                .select_for_update()
                .get(pk=participant.pk)
            )
        except Exception:
            locked = participant

        if locked.disqualified_for_sha_mismatch:
            report["already_disqualified"] = True
            return report

        update_fields = ["disqualified_for_sha_mismatch", "eliminated", "ready"]
        locked.disqualified_for_sha_mismatch = True
        locked.eliminated = True
        # Unregister the offender from the active roster for every
        # tournament type. The lobby views filter on ready/eliminated,
        # so flipping these makes the player vanish from the lineup.
        locked.ready = False

        # Persist the reason if the model supports it (added in a
        # later migration; getattr-guarded for older DBs).
        if hasattr(locked, "disqualified_reason"):
            locked.disqualified_reason = reason
            update_fields.append("disqualified_reason")

        report["removed_from_qa_lobby"] = is_qa

        locked.save(update_fields=update_fields)
        # Mirror onto the caller's instance so subsequent reads are
        # consistent without an extra refresh_from_db().
        participant.disqualified_for_sha_mismatch = True
        participant.eliminated = True
        participant.ready = False

    # ── Reset model integrity counters for the offender only ───────────
    # Targets the specific user's row — never a global .update() across all
    # participants.  Also runs for QA so the offender faces the same cooldown.
    try:
        from django.db.models import F
        from apps.users.models import UserGameModel
        update_kwargs = {
            "model_integrity_ok": False,
            "rated_games_since_revalidation": 0,
            "repo_changed": True,
        }
        # Roll forward only the SHA whose repo triggered this disqualification:
        # the previous "new" SHA becomes the "before change" SHA.
        # Match case-insensitively so labels like "HF Space" (registration
        # audit) and "space" (mid-round audit) both route to the space fields.
        reason_lc = (reason or "").lower()
        if "data repo" in reason_lc:
            update_kwargs["current_data_repo_sha"] = F("new_data_repo_sha")
            if new_sha:
                update_kwargs["new_data_repo_sha"] = new_sha
        elif "space" in reason_lc:
            update_kwargs["current_space_sha"] = F("new_space_sha")
            if new_sha:
                update_kwargs["new_space_sha"] = new_sha
        else:
            # model repo change
            update_kwargs["current_repo_sha"] = F("new_repo_sha")
            if new_sha:
                update_kwargs["new_repo_sha"] = new_sha
        UserGameModel.objects.filter(
            user=participant.user,
            game_type=tournament.game_type,
        ).update(**update_kwargs)
    except Exception:
        log.debug(
            "disqualify_for_repo_change: could not reset UserGameModel "
            "integrity flags for user=%s game_type=%s",
            participant.user.username, tournament.game_type,
            exc_info=True,
        )

    # ── Loud terminal log ───────────────────────────────────────
    banner = (
        "\n"
        + "*" * 78 + "\n"
        + "**  REPO CHANGE DISQUALIFICATION\n"
        + f"**  User       : {participant.user.username} (id={participant.user_id})\n"
        + f"**  Tournament : #{tournament.pk} {tournament.name!r} "
        + f"(type={tournament.type})\n"
        + f"**  Reason     : {reason}\n"
        + (
            "**  QA action  : participant removed from lobby (ready=False, eliminated=True)\n"
            if is_qa else
            "**  Action     : participant eliminated from bracket\n"
        )
        + "*" * 78 + "\n"
    )
    try:
        print(banner, flush=True)
    except UnicodeEncodeError:
        print(banner.encode("ascii", "replace").decode("ascii"), flush=True)
    log.warning(
        "disqualify_for_repo_change: user=%s tournament=%s qa=%s reason=%s",
        participant.user.username, tournament.pk, is_qa, reason,
    )

    # ── Notify the user's live match page via WebSocket ────────────
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"user_{participant.user_id}",
                {
                    "type": "user.disqualified",
                    "tournament_name": tournament.name,
                    "reason": "Your repository SHA changed during a live tournament.",
                },
            )
    except Exception:
        log.debug(
            "disqualify_for_repo_change: WS push failed for user=%s",
            participant.user.username, exc_info=True,
        )

    # ── Forfeit live game (outside the atomic block so the
    #     long-running side effects don't hold the row lock) ────
    if forfeit_live_match:
        report["forfeit_attempted"] = True
        try:
            from apps.tournaments.engine import _handle_mid_round_disqualification
            _handle_mid_round_disqualification(tournament, participant, reason)
            report["forfeit_ok"] = True
        except Exception:
            log.exception(
                "disqualify_for_repo_change: forfeit failed for user=%s",
                participant.user.username,
            )

    return report


def find_active_dq_participant(user) -> Optional[object]:
    """Return the user's TournamentParticipant row that should trap
    them on the disqualified page, or ``None`` if they're free.

    A row "traps" the user when it satisfies all of:
        * ``disqualified_for_sha_mismatch=True``
        * ``tournament.status == ONGOING``

    We deliberately do NOT trap users whose tournament is OPEN or
    FULL: a never-started QA tournament or a stale FULL row would
    otherwise keep an honest user stuck on the disqualified page
    indefinitely with no way out. Only an ACTIVELY-RUNNING tournament
    can hold a user hostage; the moment it ends (or never starts and
    is cleaned up) the user is freed automatically.

    Used by ``DisqualificationInterceptMiddleware``. Kept on this
    module so the "what counts as still-trapped?" rule lives next
    to the "how do we trap them?" rule.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    from apps.tournaments.models import Tournament, TournamentParticipant

    return (
        TournamentParticipant.objects
        .filter(
            user=user,
            disqualified_for_sha_mismatch=True,
            tournament__status=Tournament.Status.ONGOING,
        )
        .select_related("tournament")
        .order_by("-tournament__start_time")
        .first()
    )
