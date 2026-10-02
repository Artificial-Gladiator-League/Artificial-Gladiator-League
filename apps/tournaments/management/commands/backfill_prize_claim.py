from django.core.management.base import BaseCommand, CommandError

from apps.tournaments.models import PrizeClaim, Tournament


class Command(BaseCommand):
    help = (
        "Manually create a PrizeClaim for a completed money tournament that is missing one. "
        "Use this to recover tournaments where _create_prize_claim() silently failed, "
        "or to create the claim after a prize hold has been released."
    )

    def add_arguments(self, parser):
        parser.add_argument("tournament_pk", type=int, help="PK of the Tournament to backfill.")

    def handle(self, *args, **options):
        pk = options["tournament_pk"]
        try:
            t = Tournament.objects.select_related("champion").get(pk=pk)
        except Tournament.DoesNotExist:
            raise CommandError(f"Tournament pk={pk} not found.")

        if not t.is_money_tournament:
            raise CommandError(f"Tournament pk={pk} ({t.name!r}) is not a money tournament.")
        if t.status != Tournament.Status.COMPLETED:
            raise CommandError(
                f"Tournament pk={pk} ({t.name!r}) is not completed (status={t.status!r})."
            )
        if not t.champion:
            raise CommandError(f"Tournament pk={pk} ({t.name!r}) has no champion set.")
        if not t.prize_amount:
            raise CommandError(
                f"Tournament pk={pk} ({t.name!r}) has no prize_amount configured — "
                "set it in the admin before backfilling."
            )
        if t.prize_on_hold:
            raise CommandError(
                f"Tournament pk={pk} ({t.name!r}) has its prize on hold — release the hold "
                "in the admin first."
            )
        if PrizeClaim.objects.filter(tournament=t).exists():
            raise CommandError(
                f"Tournament pk={pk} ({t.name!r}) already has a PrizeClaim row. "
                "Nothing to backfill."
            )

        from apps.tournaments.engine import _create_prize_claim

        self.stdout.write(
            f"Creating PrizeClaim for {t.name!r} (pk={pk}), "
            f"champion={t.champion.username!r}, prize={t.prize_amount} {t.prize_currency}…"
        )
        _create_prize_claim(t, t.champion)
        claim = PrizeClaim.objects.filter(tournament=t).first()
        if claim is None:
            raise CommandError(
                "No PrizeClaim was created — check the log (missing currency for a non-Israeli "
                "tournament, or a failure while creating it)."
            )
        self.stdout.write(self.style.SUCCESS(
            f"Done — PrizeClaim created, claim window {t.claim_deadline_days} days "
            f"(expires {claim.expires_at:%Y-%m-%d}), winner email sent."
        ))
