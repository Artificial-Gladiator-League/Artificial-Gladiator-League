# ──────────────────────────────────────────────
# Management command: create_qa_tournament
#
# Creates a QA tournament with capacity=2 that
# starts as soon as 2 players register.
#
# Usage:
#   python manage.py create_qa_tournament
#   python manage.py create_qa_tournament --allowed-countries IL,IN --money \
#       --terms-slug <slug> --prize-amount 100 --currency INR
#   python manage.py create_qa_tournament --allowed-countries all
# ──────────────────────────────────────────────
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.countries import normalize_country_codes
from apps.tournaments.models import Tournament, TournamentTerms


class Command(BaseCommand):
    help = "Create a QA tournament (2 players, 1 round) for testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allowed-countries", default="IL",
            help='Comma-separated ISO country codes (e.g. "IL,IN") or "all" for every country. Default: IL.',
        )
        parser.add_argument(
            "--money", action="store_true",
            help="Run the real money flow (terms page, geo gate, confirmation PDF). "
                 "A completed money tournament creates a REAL prize claim and emails the winner.",
        )
        parser.add_argument("--terms-slug", help="Link the newest active terms record with this slug.")
        parser.add_argument("--prize-amount", help="Prize amount, e.g. 100.")
        parser.add_argument("--currency", help="ISO 4217 prize currency, e.g. INR.")

    def handle(self, *args, **options):
        raw = (options["allowed_countries"] or "").strip()
        if raw.lower() == "all":
            countries = []
        else:
            countries = normalize_country_codes(raw)
            if not countries:
                raise CommandError('--allowed-countries needs country codes or "all".')

        terms = None
        if options["terms_slug"]:
            terms = (
                TournamentTerms.objects
                .filter(slug=options["terms_slug"], is_active=True)
                .order_by("-created_at")
                .first()
            )
            if terms is None:
                raise CommandError(f'No active terms record with slug "{options["terms_slug"]}".')

        prize_amount = None
        if options["prize_amount"] is not None:
            try:
                prize_amount = Decimal(options["prize_amount"])
            except InvalidOperation:
                raise CommandError("--prize-amount must be a number.")
        if options["money"] and prize_amount is None and (terms is None or terms.has_prize):
            raise CommandError(
                "--money needs --prize-amount: a completed money tournament creates a real "
                "prize claim (or mails the admins when there is no prize)."
            )

        now = timezone.now()
        name = f"QA Test Tournament — {now.strftime('%Y-%m-%d %H:%M')}"
        tournament = Tournament(
            name=name,
            type=Tournament.Type.QA,
            category=Tournament.Category.BEGINNER,
            capacity=2,
            rounds_total=1,
            start_time=now + timedelta(minutes=5),
            status=Tournament.Status.OPEN,
            allowed_countries=countries,
            is_money_tournament=options["money"],
            terms=terms,
            prize_amount=prize_amount,
        )
        if options["currency"]:
            tournament.prize_currency = options["currency"]

        try:
            tournament.full_clean()
        except ValidationError as exc:
            problems = "; ".join(f"{field}: {' '.join(msgs)}" for field, msgs in exc.message_dict.items())
            raise CommandError(f"Invalid tournament: {problems}")
        tournament.save()

        self.stdout.write(self.style.SUCCESS(
            f"Created QA tournament: {tournament.name} "
            f"(id={tournament.pk}, capacity={tournament.capacity}, "
            f"rounds={tournament.rounds_total})"
        ))
        scope = "all countries" if tournament.is_open_to_all else ", ".join(tournament.allowed_country_codes)
        self.stdout.write(
            f"Countries: {scope} | money: {'yes' if tournament.is_money_tournament else 'no'} | "
            f"terms: {terms or 'none'}"
        )
        base = getattr(settings, "SITE_URL", "").rstrip("/")
        self.stdout.write(f"Detail : {base}{reverse('tournaments:detail', args=[tournament.pk])}")
        if tournament.is_money_tournament:
            self.stdout.write(f"Terms  : {base}{reverse('tournaments:money_terms', args=[tournament.pk])}")
        self.stdout.write(f"Admin  : {base}{reverse('admin:tournaments_tournament_change', args=[tournament.pk])}")
        self.stdout.write("Register 2 players to trigger auto-start.")
        if tournament.is_money_tournament:
            self.stdout.write(self.style.WARNING(
                "Money flow: private IPs skip the geo gate, so set user.country for each QA user "
                "(one allowed, one not) to exercise the profile-country check."
            ))
