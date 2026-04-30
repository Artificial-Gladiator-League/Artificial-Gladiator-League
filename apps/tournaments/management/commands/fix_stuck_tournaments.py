"""Repair stuck tournaments and re-arm anti-cheat enforcement.

Usage::

    python manage.py fix_stuck_tournaments              # sweep all
    python manage.py fix_stuck_tournaments --tournament 42
    python manage.py fix_stuck_tournaments --dry-run

The command is idempotent — running it twice in a row produces no
extra side-effects on a healthy tournament.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Repair stuck tournaments and arm SHA integrity enforcement."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tournament", type=int, default=None,
            help="Only fix this tournament id (default: sweep all).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would happen without persisting changes.",
        )

    def handle(self, *args, **opts):
        from apps.tournaments.lifecycle import (
            ensure_all_open_tournaments,
            ensure_tournament_integrity,
        )
        from apps.tournaments.models import Tournament

        if opts["dry_run"]:
            # Re-export the sweep without touching DB by wrapping in a
            # rolled-back transaction.
            from django.db import transaction

            with transaction.atomic():
                sid = transaction.savepoint()
                try:
                    reports = self._run(opts, ensure_tournament_integrity,
                                        ensure_all_open_tournaments)
                finally:
                    transaction.savepoint_rollback(sid)
            self.stdout.write(self.style.WARNING(
                "DRY RUN — all changes rolled back."
            ))
        else:
            reports = self._run(opts, ensure_tournament_integrity,
                                ensure_all_open_tournaments)

        self._print_reports(reports)

    def _run(self, opts, fix_one, fix_all):
        from apps.tournaments.models import Tournament

        if opts["tournament"]:
            try:
                t = Tournament.objects.get(pk=opts["tournament"])
            except Tournament.DoesNotExist:
                raise CommandError(
                    f"Tournament {opts['tournament']} does not exist."
                )
            return [fix_one(t)]
        return fix_all()

    def _print_reports(self, reports):
        if not reports:
            self.stdout.write("No tournaments matched the recovery filter.")
            return

        fixed = sum(1 for r in reports if r.get("actions"))
        self.stdout.write(self.style.SUCCESS(
            f"Scanned {len(reports)} tournament(s); "
            f"{fixed} had recovery actions applied."
        ))
        for r in reports:
            actions = r.get("actions") or []
            line = (
                f"  • #{r['tournament_id']} {r.get('tournament_name', '?')}: "
                f"status {r.get('status_before')} -> {r.get('status_after')}, "
                f"round {r.get('current_round_before')} -> "
                f"{r.get('current_round_after')}, "
                f"actions={','.join(actions) or 'none'}"
            )
            if r.get("errors"):
                line += f" errors={r['errors']}"
                self.stdout.write(self.style.ERROR(line))
            elif actions:
                self.stdout.write(self.style.SUCCESS(line))
            else:
                self.stdout.write(line)
