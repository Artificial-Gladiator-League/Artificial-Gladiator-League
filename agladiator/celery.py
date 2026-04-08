# ──────────────────────────────────────────────
# agladiator/celery.py — Celery application bootstrap
#
# Start the worker:
#   celery -A agladiator worker -l info
#
# Start the beat scheduler:
#   celery -A agladiator beat -l info
# ──────────────────────────────────────────────
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agladiator.settings")

app = Celery("agladiator")

# Read config from Django settings, using the CELERY_ namespace.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all installed apps.
app.autodiscover_tasks()
