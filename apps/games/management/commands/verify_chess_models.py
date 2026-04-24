from django.core.management.base import BaseCommand

from apps.users.models import UserGameModel
from apps.games.hf_inference import verify_model


class Command(BaseCommand):
    help = "Verify all chess UserGameModel entries via the HF API integrity check."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force verification even if model is already approved",
        )
        parser.add_argument(
            "--user",
            type=int,
            help="Only verify models belonging to this user id",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List models that would be verified without running verification",
        )

    def handle(self, *args, **options):
        qs = UserGameModel.objects.filter(game_type="chess").exclude(hf_model_repo_id="")
        if options.get("user"):
            qs = qs.filter(user_id=options.get("user"))

        total = qs.count()
        if total == 0:
            self.stdout.write("No chess models found to verify.")
            return

        self.stdout.write(f"Found {total} chess model(s) to verify")

        for gm in qs.order_by("user_id"):
            self.stdout.write(
                f"Verifying: UserGameModel(id={gm.id} user={gm.user_id} repo={gm.hf_model_repo_id})"
            )
            if options.get("dry_run"):
                continue

            try:
                passed, msg, report = verify_model(gm, force=options.get("force", False))
            except Exception as exc:
                self.stderr.write(
                    f"ERROR verifying {gm.hf_model_repo_id} (user {gm.user_id}): {exc}"
                )
                continue

            if passed:
                self.stdout.write(self.style.SUCCESS(f"OK: {gm.hf_model_repo_id} — {msg}"))
            else:
                self.stdout.write(self.style.ERROR(f"FAILED: {gm.hf_model_repo_id} — {msg}"))
