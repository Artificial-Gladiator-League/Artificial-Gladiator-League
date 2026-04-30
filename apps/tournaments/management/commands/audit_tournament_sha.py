"""Manually trigger the anti-cheat SHA audit.

Examples
--------

Run a single check for one participant::

    python manage.py audit_tournament_sha --tournament 12 --user 143

Run a full random pass (same algorithm as the Celery beat task)::

    python manage.py audit_tournament_sha --random-pass

Pin a fresh round baseline for every active participant in a tournament
(useful after manual data fixes)::

    python manage.py audit_tournament_sha --tournament 12 --capture-baseline
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Manually trigger anti-cheat SHA verification for tournament participants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tournament", type=int, default=None,
            help="Tournament ID to operate on.",
        )
        parser.add_argument(
            "--user", type=int, default=None,
            help="User ID — limit checks to this participant.",
        )
        parser.add_argument(
            "--capture-baseline", action="store_true",
            help="Re-pin round baseline SHAs for the tournament's current round.",
        )
        parser.add_argument(
            "--random-pass", action="store_true",
            help="Run one randomised audit pass across ALL ongoing tournaments.",
        )
        parser.add_argument(
            "--tick-seconds", type=float, default=30.0,
            help="Tick period (seconds) used by --random-pass to compute Bernoulli p.",
        )

    def handle(self, *args, **opts):
        from apps.tournaments.models import Tournament, TournamentParticipant
        from apps.tournaments.sha_audit import (
            capture_round_baseline,
            perform_sha_check,
            run_random_audit_pass,
        )

        if opts["random_pass"]:
            # Run inline so the operator sees results in this terminal.
            summary = run_random_audit_pass(
                tick_seconds=opts["tick_seconds"], async_dispatch=False,
            )
            self.stdout.write(self.style.SUCCESS(
                f"Random pass: tournaments={summary['tournaments']} "
                f"checked={summary['checked']} skipped={summary['skipped']}"
            ))
            for r in summary["results"]:
                self.stdout.write(f"  • {r['user']} ({r['tournament']}) → {r['result']}")
            return

        tournament_id = opts["tournament"]
        if tournament_id is None:
            raise CommandError(
                "Specify --tournament <id> (or use --random-pass)."
            )
        try:
            tournament = Tournament.objects.get(pk=tournament_id)
        except Tournament.DoesNotExist:
            raise CommandError(f"Tournament {tournament_id} does not exist.")

        if opts["capture_baseline"]:
            n = capture_round_baseline(tournament, tournament.current_round or 0)
            self.stdout.write(self.style.SUCCESS(
                f"Pinned baseline SHA for {n} participant(s) in '{tournament.name}'."
            ))
            return

        qs = tournament.participants.select_related("user")
        if opts["user"] is not None:
            qs = qs.filter(user_id=opts["user"])
            if not qs.exists():
                raise CommandError(
                    f"User {opts['user']} is not a participant of tournament {tournament_id}."
                )

        passed = failed = errored = 0
        for p in qs:
            row = perform_sha_check(p, context="manual")
            if row is None:
                errored += 1
                continue
            if row.result == "pass":
                passed += 1
            elif row.result == "fail":
                failed += 1
            else:
                errored += 1

        self.stdout.write(self.style.SUCCESS(
            f"Manual SHA audit done — pass={passed} fail={failed} error/skip={errored}"
        ))
