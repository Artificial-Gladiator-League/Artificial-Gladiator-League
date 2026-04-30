"""Management command: verify_repo_ownership

Usage examples
──────────────
# Generate a new challenge code for a user's game model
python manage.py verify_repo_ownership --user-id 42 --game-type chess --generate

# Check the AGL_VERIFY.txt file in the repo and mark verified if it matches
python manage.py verify_repo_ownership --user-id 42 --game-type chess --check

# Re-verify all participants currently registered in an active tournament
python manage.py verify_repo_ownership --tournament-id 7 --check
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Generate or check Proof-of-Ownership (AGL_VERIFY.txt) for a user's game model."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--generate",
            action="store_true",
            help="Generate (or regenerate) the verification challenge code.",
        )
        group.add_argument(
            "--check",
            action="store_true",
            help="Check AGL_VERIFY.txt in the HF repo and mark is_verified=True if correct.",
        )

        target = parser.add_mutually_exclusive_group()
        target.add_argument("--user-id", type=int, help="Target a specific user by PK.")
        target.add_argument(
            "--tournament-id",
            type=int,
            help="Re-verify all active participants in this tournament (--check only).",
        )

        parser.add_argument(
            "--game-type",
            choices=["chess", "breakthrough"],
            default=None,
            help="Game type to target (required with --user-id).",
        )
        parser.add_argument(
            "--all-game-types",
            action="store_true",
            help="Run for both game types when used with --user-id.",
        )

    # ─────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        from apps.users.models import UserGameModel
        from apps.users.ownership_verification import (
            check_ownership,
            generate_verification_code,
            re_verify_ownership,
        )

        if options["tournament_id"]:
            if not options["check"]:
                raise CommandError("--tournament-id can only be used with --check.")
            self._handle_tournament(options["tournament_id"])
            return

        # ── Single user mode ──────────────────────────────────
        if not options["user_id"]:
            raise CommandError("Provide --user-id or --tournament-id.")

        user_id = options["user_id"]
        game_types: list[str]
        if options["all_game_types"]:
            game_types = ["chess", "breakthrough"]
        elif options["game_type"]:
            game_types = [options["game_type"]]
        else:
            raise CommandError("Provide --game-type or --all-game-types.")

        try:
            from apps.users.models import CustomUser
            user = CustomUser.objects.get(pk=user_id)
        except CustomUser.DoesNotExist:
            raise CommandError(f"User id={user_id} not found.")

        for game_type in game_types:
            try:
                gm = UserGameModel.objects.get(user=user, game_type=game_type)
            except UserGameModel.DoesNotExist:
                self.stderr.write(
                    self.style.WARNING(
                        f"  No {game_type} model registered for {user.username} — skipping."
                    )
                )
                continue

            if options["generate"]:
                code = generate_verification_code(gm)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n[{user.username}] {game_type} — Challenge code generated:\n"
                        f"\n  {code}\n\n"
                        f"  Ask the user to create a file named '{from_verify_filename()}' "
                        f"at the root of their HF repo\n"
                        f"  ({gm.hf_model_repo_id}) containing exactly that code.\n"
                        f"  Then run:  manage.py verify_repo_ownership "
                        f"--user-id {user_id} --game-type {game_type} --check\n"
                    )
                )

            elif options["check"]:
                ok, msg = check_ownership(gm)
                if ok:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"[{user.username}] {game_type} — VERIFIED: {msg}"
                        )
                    )
                else:
                    self.stderr.write(
                        self.style.ERROR(
                            f"[{user.username}] {game_type} — FAILED: {msg}"
                        )
                    )

    # ─────────────────────────────────────────────────────────────

    def _handle_tournament(self, tournament_id: int) -> None:
        from apps.tournaments.models import TournamentParticipant
        from apps.users.models import UserGameModel
        from apps.users.ownership_verification import re_verify_ownership

        participants = list(
            TournamentParticipant.objects.filter(
                tournament_id=tournament_id,
            ).select_related("user")
        )
        if not participants:
            self.stdout.write(f"No participants in tournament id={tournament_id}.")
            return

        try:
            from apps.tournaments.models import Tournament
            tournament = Tournament.objects.get(pk=tournament_id)
            game_type = tournament.game_type
        except Tournament.DoesNotExist:
            raise CommandError(f"Tournament id={tournament_id} not found.")

        self.stdout.write(
            f"Re-verifying {len(participants)} participants "
            f"(tournament={tournament.name}, game_type={game_type})..."
        )

        for p in participants:
            try:
                gm = UserGameModel.objects.get(user=p.user, game_type=game_type)
            except UserGameModel.DoesNotExist:
                self.stderr.write(
                    self.style.WARNING(f"  {p.user.username}: no {game_type} model registered.")
                )
                continue

            ok, msg = re_verify_ownership(gm)
            style = self.style.SUCCESS if ok else self.style.ERROR
            prefix = "OK   " if ok else "FAIL "
            self.stdout.write(style(f"  {prefix} {p.user.username}: {msg}"))


def from_verify_filename() -> str:
    from apps.users.ownership_verification import VERIFY_FILENAME
    return VERIFY_FILENAME
