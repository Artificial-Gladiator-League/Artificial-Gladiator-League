"""Clear stale ``disqualified_for_sha_mismatch`` flags.

Use when a user (e.g. yuval) was incorrectly disqualified by an
earlier audit run and is now stuck on the disqualified.html page.

Usage:
    python manage.py clear_dq --username yuval
    python manage.py clear_dq --user-id 42
    python manage.py clear_dq --username yuval --tournament-id 5
    python manage.py clear_dq --username yuval --all-tournaments

By default only rows on ONGOING tournaments are cleared (those are
the ones the middleware traps on). Pass --all-tournaments to clear
every DQ flag across the user's history.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Clear stale anti-cheat disqualification flags for a user."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, default=None)
        parser.add_argument("--user-id", type=int, default=None)
        parser.add_argument(
            "--tournament-id", type=int, default=None,
            help="Only clear the flag on this specific tournament.",
        )
        parser.add_argument(
            "--all-tournaments", action="store_true",
            help=(
                "Clear DQ flags on the user's rows in EVERY tournament "
                "(including non-ONGOING ones). Default clears only "
                "ONGOING — i.e. the rows that actually trap the user."
            ),
        )

    def handle(self, *args, **opts):
        from apps.tournaments.models import Tournament, TournamentParticipant
        from apps.users.models import CustomUser

        username = opts.get("username")
        user_id = opts.get("user_id")
        tournament_id = opts.get("tournament_id")
        all_tournaments = opts.get("all_tournaments")

        if not username and not user_id:
            raise CommandError("Provide --username or --user-id.")

        try:
            user = (
                CustomUser.objects.get(username=username)
                if username else CustomUser.objects.get(pk=user_id)
            )
        except CustomUser.DoesNotExist:
            raise CommandError(f"User not found: {username or user_id}")

        qs = TournamentParticipant.objects.filter(
            user=user, disqualified_for_sha_mismatch=True,
        )
        if tournament_id is not None:
            qs = qs.filter(tournament_id=tournament_id)
        elif not all_tournaments:
            qs = qs.filter(tournament__status=Tournament.Status.ONGOING)

        rows = list(qs.select_related("tournament"))
        if not rows:
            self.stdout.write(
                f"No matching DQ rows for {user.username}. Nothing to clear."
            )
            return

        for p in rows:
            self.stdout.write(
                f"Clearing DQ flag — user={user.username} "
                f"tournament=#{p.tournament.pk} {p.tournament.name!r} "
                f"(status={p.tournament.status})"
            )
            update_fields = ["disqualified_for_sha_mismatch"]
            p.disqualified_for_sha_mismatch = False
            # Also unblock them from the bracket if eliminated solely
            # because of the SHA flag and the tournament hasn't already
            # marked them eliminated_in_round.
            if p.eliminated and p.eliminated_in_round is None:
                p.eliminated = False
                update_fields.append("eliminated")
            if hasattr(p, "disqualified_reason"):
                p.disqualified_reason = ""
                update_fields.append("disqualified_reason")
            p.save(update_fields=update_fields)

        self.stdout.write(
            f"Done. Cleared {len(rows)} DQ row(s) for {user.username}."
        )
