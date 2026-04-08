# ──────────────────────────────────────────────
# Management command: create_qa_tournament
#
# Creates a QA tournament with capacity=2 that
# starts as soon as 2 players register.
#
# Usage:
#   python manage.py create_qa_tournament
# ──────────────────────────────────────────────
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tournaments.models import Tournament


class Command(BaseCommand):
    help = "Create a QA tournament (2 players, 1 round) for testing."

    def handle(self, *args, **options):
        now = timezone.now()
        name = f"QA Test Tournament — {now.strftime('%Y-%m-%d %H:%M')}"

        tournament = Tournament.objects.create(
            name=name,
            type=Tournament.Type.QA,
            category=Tournament.Category.BEGINNER,
            prize_pool=Decimal("0.00"),
            start_time=now + timedelta(minutes=5),
            status=Tournament.Status.OPEN,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created QA tournament: {tournament.name} "
            f"(id={tournament.pk}, capacity={tournament.capacity}, "
            f"rounds={tournament.rounds_total})"
        ))
        self.stdout.write(
            "Register 2 players to trigger auto-start."
        )
