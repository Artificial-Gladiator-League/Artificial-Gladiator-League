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
        start_tournament(t)
        started.append(t.name)
        log.info("Auto-started FULL tournament: %s", t.name)

    # Handle OPEN tournaments past their start_time
    for t in Tournament.objects.filter(
        status=Tournament.Status.OPEN,
        start_time__lte=now,
    ):
        if t.participant_count >= 2:
            start_tournament(t)
            started.append(t.name)
            log.info("Auto-started overdue tournament: %s (%d players)", t.name, t.participant_count)
        else:
            t.status = Tournament.Status.COMPLETED
            t.save(update_fields=["status"])
            log.info("Aborted tournament with <2 players: %s", t.name)

    return f"Checked tournaments. Started: {started or 'none'}"


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
