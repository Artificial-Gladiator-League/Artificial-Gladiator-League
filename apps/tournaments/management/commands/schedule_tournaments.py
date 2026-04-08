# ──────────────────────────────────────────────
# Management command: schedule_tournaments
#
# Creates the weekly tournament slate:
#   • 4 small (128-player) tournaments — one per ELO category
#   • 1 large (1024-player) tournament — rotating category
#
# Run manually:
#   python manage.py schedule_tournaments
#
# Or via cron / Celery beat (weekly):
#   celery -A agladiator beat
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tournaments.models import Tournament

log = logging.getLogger(__name__)

# Category rotation order for the weekly large tournament
LARGE_ROTATION = [
    Tournament.Category.BEGINNER,
    Tournament.Category.INTERMEDIATE,
    Tournament.Category.ADVANCED,
    Tournament.Category.EXPERT,
]

SMALL_PRIZE = Decimal("15.00")
LARGE_PRIZE = Decimal("60.00")  # $30 + $20 + $10


class Command(BaseCommand):
    help = (
        "Create the weekly tournament slate: "
        "4 small (per-category) + 1 large (rotating category)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without touching the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        # ── Small tournaments (one per category) ────────
        for cat_value, cat_label in Tournament.Category.choices:
            name = f"Weekly {cat_label} (Small) — {now.strftime('%Y-W%W')}"

            if Tournament.objects.filter(
                name=name, type=Tournament.Type.SMALL,
            ).exists():
                self.stdout.write(self.style.WARNING(f"  SKIP (exists): {name}"))
                continue

            if dry_run:
                self.stdout.write(self.style.NOTICE(f"  DRY-RUN: {name}"))
                continue

            Tournament.objects.create(
                name=name,
                type=Tournament.Type.SMALL,
                category=cat_value,
                prize_pool=SMALL_PRIZE,
                start_time=now + timedelta(hours=1),
                status=Tournament.Status.OPEN,
            )
            self.stdout.write(self.style.SUCCESS(f"  CREATED: {name}"))

        # ── Large tournament (rotating category) ────────
        last_large = (
            Tournament.objects
            .filter(type=Tournament.Type.LARGE)
            .order_by("-created_at")
            .first()
        )
        if last_large and last_large.category:
            try:
                idx = LARGE_ROTATION.index(last_large.category)
                next_cat = LARGE_ROTATION[(idx + 1) % len(LARGE_ROTATION)]
            except ValueError:
                next_cat = LARGE_ROTATION[0]
        else:
            next_cat = LARGE_ROTATION[0]

        large_name = (
            f"Weekly {Tournament.Category(next_cat).label} (Large) "
            f"— {now.strftime('%Y-W%W')}"
        )

        if Tournament.objects.filter(
            name=large_name, type=Tournament.Type.LARGE,
        ).exists():
            self.stdout.write(self.style.WARNING(f"  SKIP (exists): {large_name}"))
        elif dry_run:
            self.stdout.write(self.style.NOTICE(f"  DRY-RUN: {large_name}"))
        else:
            Tournament.objects.create(
                name=large_name,
                type=Tournament.Type.LARGE,
                category=next_cat,
                prize_pool=LARGE_PRIZE,
                start_time=now + timedelta(hours=2),
                status=Tournament.Status.OPEN,
            )
            self.stdout.write(self.style.SUCCESS(f"  CREATED: {large_name}"))

        # ── Auto-start any FULL tournaments that haven't started yet ──
        full_tournaments = Tournament.objects.filter(status=Tournament.Status.FULL)
        for t in full_tournaments:
            if dry_run:
                self.stdout.write(
                    self.style.NOTICE(f"  DRY-RUN: would start {t.name}")
                )
                continue

            from apps.tournaments.engine import start_tournament
            start_tournament(t)
            self.stdout.write(
                self.style.SUCCESS(f"  STARTED: {t.name} (round 1)")
            )

        self.stdout.write(self.style.SUCCESS("\nDone."))
