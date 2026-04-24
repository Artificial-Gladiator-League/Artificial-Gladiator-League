# ──────────────────────────────────────────────
# apps/tournaments/tasks.py
#
# Celery task stubs for automated tournament
# lifecycle management.  Wire these into your
# celery beat schedule in settings.py:
#
#   CELERY_BEAT_SCHEDULE = {
#       "schedule-tournaments-weekly": {
#           "task": "apps.tournaments.tasks.schedule_weekly_tournaments",
#           "schedule": crontab(day_of_week="monday", hour=0, minute=0),
#       },
#       "check-stale-tournaments": {
#           "task": "apps.tournaments.tasks.check_stale_tournaments",
#           "schedule": crontab(minute="*/5"),
#       },
#   }
# ──────────────────────────────────────────────
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Attempt Celery import; if unavailable, tasks are plain functions
# callable from management commands or cron scripts.
try:
    from celery import shared_task
except ImportError:
    def shared_task(func=None, **kwargs):
        """No-op decorator when Celery is not installed."""
        if func is not None:
            return func
        return lambda f: f


@shared_task
def schedule_weekly_tournaments() -> str:
    """Create the weekly slate of tournaments.

    Equivalent to:  python manage.py schedule_tournaments
    """
    from django.core.management import call_command
    call_command("schedule_tournaments")
    return "Weekly tournaments scheduled."


@shared_task
def check_stale_tournaments() -> str:
    """Auto-start FULL tournaments that haven't kicked off yet.

    Also detects tournaments past their start_time that are still OPEN
    and either starts them (if ≥ 2 players) or aborts them.
    """
    from django.utils import timezone

    from apps.tournaments.engine import start_tournament
    from apps.tournaments.models import Tournament

    now = timezone.now()
    started = []

    # Start any tournament marked FULL
    for t in Tournament.objects.filter(status=Tournament.Status.FULL):
        _integrity_gate_and_start(t, started)
        log.info("Auto-started FULL tournament: %s", t.name)

    # Handle OPEN tournaments past their start_time
    for t in Tournament.objects.filter(
        status=Tournament.Status.OPEN,
        start_time__lte=now,
    ):
        if t.participant_count >= 2:
            _integrity_gate_and_start(t, started)
            log.info("Auto-started overdue tournament: %s (%d players)", t.name, t.participant_count)
        else:
            t.status = Tournament.Status.COMPLETED
            t.save(update_fields=["status"])
            log.info("Aborted tournament with <2 players: %s", t.name)

    return f"Checked tournaments. Started: {started or 'none'}"


def _integrity_gate_and_start(tournament, started: list) -> None:
    """Run integrity checks then start *tournament*, appending its name to *started*."""
    from apps.tournaments.engine import start_tournament
    # Run synchronous integrity gate (fire-and-forget already happened in the task)
    try:
        result = run_pre_tournament_integrity_checks(tournament.pk)
        failed = result.get("failed", [])
        if failed:
            log.warning(
                "Tournament %s: %d participant(s) removed after integrity failure before start: %s",
                tournament.name, len(failed), [f["user_id"] for f in failed],
            )
    except Exception:
        log.exception("Integrity gate failed for tournament %s — starting anyway", tournament.name)
    start_tournament(tournament)
    started.append(tournament.name)


@shared_task
def run_gladiator_gauntlet(
    participants: int = 16,
    rounds: int = 5,
    time_control: str = "3+1",
) -> str:
    """Run a full Gladiator Gauntlet tournament.

    This is the Celery-friendly wrapper around the management command.
    Schedule it with Celery Beat to fire every Sunday at 20:00 UTC.
    """
    from django.core.management import call_command

    call_command(
        "run_gauntlet",
        participants=participants,
        rounds=rounds,
        time_control=time_control,
    )
    return "Gladiator Gauntlet completed."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-tournament integrity gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@shared_task(bind=True, max_retries=0)
def run_pre_tournament_integrity_checks(self, tournament_id: int) -> dict:
    """Verify local model file integrity for every participant of a tournament.

    Called automatically before a tournament is started (see
    ``check_stale_tournaments``).  Participants whose models fail the
    integrity check have their entry removed and are notified.

    Returns a summary dict::

        {
            "tournament_id": <int>,
            "passed": [<user_id>, ...],
            "failed": [{"user_id": <int>, "reason": <str>}, ...],
        }
    """
    from apps.tournaments.models import Tournament, TournamentParticipant
    from apps.users.models import UserGameModel
    from apps.users.integrity import check_local_integrity

    try:
        tournament = Tournament.objects.get(pk=tournament_id)
    except Tournament.DoesNotExist:
        log.error("run_pre_tournament_integrity_checks: tournament %s not found", tournament_id)
        return {"tournament_id": tournament_id, "passed": [], "failed": [], "error": "not found"}

    game_type = getattr(tournament, "game_type", "chess")
    participants = list(
        TournamentParticipant.objects.select_related("user")
        .filter(tournament=tournament)
    )

    passed: list[int] = []
    failed: list[dict] = []

    for participant in participants:
        user_id = participant.user_id
        gm = UserGameModel.objects.filter(user_id=user_id, game_type=game_type).first()
        if gm is None:
            # No model configured — nothing to check (default AI / no user model).
            passed.append(user_id)
            continue

        ok, reason = check_local_integrity(gm, alert_admins=True)
        if ok:
            passed.append(user_id)
            log.info(
                "Integrity OK for user=%s game=%s tournament=%s",
                user_id, game_type, tournament_id,
            )
        else:
            failed.append({"user_id": user_id, "reason": reason})
            log.warning(
                "Integrity FAIL for user=%s game=%s tournament=%s — removing from tournament: %s",
                user_id, game_type, tournament_id, reason,
            )
            # Remove participant from tournament
            try:
                participant.delete()
            except Exception:
                log.exception("Could not remove participant user=%s from tournament=%s", user_id, tournament_id)

    log.info(
        "Pre-tournament integrity check done for tournament=%s: %d passed, %d failed",
        tournament_id, len(passed), len(failed),
    )
    return {"tournament_id": tournament_id, "passed": passed, "failed": failed}
