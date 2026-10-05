# ──────────────────────────────────────────────
# apps/users/tasks.py
#
# Celery tasks for the users app (picked up by app.autodiscover_tasks()).
# The worker needs access to the Docker daemon: the diagnostics run executes
# the contract test in the sandbox.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

log = logging.getLogger(__name__)

# Slightly above SANDBOX_VERIFY_TIMEOUT (300s) so the sandbox's own timeout fires first.
DIAGNOSTICS_SOFT_TIME_LIMIT = 400
DIAGNOSTICS_HARD_TIME_LIMIT = 430

# Model downloads can be large; keep well above a normal snapshot_download.
PRELOAD_SOFT_TIME_LIMIT = 1500
PRELOAD_HARD_TIME_LIMIT = 1560


@shared_task(
    soft_time_limit=PRELOAD_SOFT_TIME_LIMIT,
    time_limit=PRELOAD_HARD_TIME_LIMIT,
)
def preload_user_models_task(user_id: int) -> None:
    from apps.games.model_preloader import preload_user_models

    try:
        preload_user_models(user_id)
    except SoftTimeLimitExceeded:
        log.error("[MODEL-DOWNLOAD-FAILED] preload_user_models timed out for user=%s", user_id)


@shared_task(
    soft_time_limit=DIAGNOSTICS_SOFT_TIME_LIMIT,
    time_limit=DIAGNOSTICS_HARD_TIME_LIMIT,
)
def run_diagnostics_task(run_id: int) -> None:
    from apps.users.diagnostics import fail_run, run_diagnostics

    try:
        run_diagnostics(run_id)
    except SoftTimeLimitExceeded:
        log.error("Diagnostics run %s exceeded the soft time limit", run_id)
        fail_run(run_id, "Diagnostics timed out before finishing. Please try again later.")
