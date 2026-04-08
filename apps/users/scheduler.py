# ──────────────────────────────────────────────
# apps/users/scheduler.py
#
# APScheduler job that runs daily at 20:00 Israel
# time (Asia/Jerusalem) to trigger batch model
# integrity checks for all users.
#
# Started once from UsersConfig.ready() when the
# server boots (not during management commands).
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from django_apscheduler.jobstores import DjangoJobStore

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _daily_integrity_job():
    """Wrapper called by APScheduler — runs the batch check."""
    log.info("⏰ APScheduler: daily integrity job triggered")
    try:
        from apps.users.integrity import run_daily_integrity_check
        stats = run_daily_integrity_check()
        log.info("⏰ APScheduler: daily integrity job finished — %s", stats)

        # Reset validation dates so every user must re-verify on next login.
        from apps.users.models import UserGameModel
        reset_count = UserGameModel.objects.filter(
            hf_model_repo_id__gt="",
        ).exclude(
            last_model_validation_date=None,
        ).update(last_model_validation_date=None)
        log.info(
            "⏰ APScheduler: reset validation dates for %d models — "
            "all users will re-verify on next login",
            reset_count,
        )
    except Exception:
        log.exception("❌ APScheduler: daily integrity job failed")


def start_scheduler():
    """Start the background scheduler with the daily integrity job.

    Safe to call multiple times — only the first call starts the scheduler.
    """
    global _scheduler
    if _scheduler is not None:
        return  # already running

    # Don't start during management commands
    is_management = any(
        cmd in sys.argv
        for cmd in (
            "migrate", "makemigrations", "collectstatic",
            "createsuperuser", "check", "shell", "dbshell",
            "showmigrations", "test", "flush",
        )
    )
    if is_management:
        log.debug("Skipping scheduler start (management command).")
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_jobstore(DjangoJobStore(), "default")

    _scheduler.add_job(
        _daily_integrity_job,
        trigger=CronTrigger(
            hour=12,
            minute=00,
            timezone="Asia/Jerusalem",
        ),
        id="daily_model_integrity_check",
        name="Daily Model Integrity Check (20:00 IDT)",
        max_instances=1,
        replace_existing=True,
    )

    _scheduler.start()
    log.info("✅ APScheduler started — daily integrity check scheduled at 20:00 Asia/Jerusalem")
