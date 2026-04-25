"""
Management command: create_endpoints_for_all_users
──────────────────────────────────────────────────
Bulk-verify models for every UserGameModel that has a linked
``hf_model_repo_id`` but has not been verified yet (or needs
re-verification).

Runs the full Docker sandbox pipeline: download → security scan →
sandbox test positions.

Also fills the HF inference endpoint fields for ALL game types:
    hf_inference_endpoint_name   — "{owner}-{game_type}"
    hf_inference_endpoint_id     — "{owner}-{game_type}"
    hf_inference_endpoint_status — "ready" | "failed" | "pending"

These fields are derived purely from the model repo ID; nothing is
hardcoded.

Usage:
    python manage.py create_endpoints_for_all_users
    python manage.py create_endpoints_for_all_users --game-type breakthrough
    python manage.py create_endpoints_for_all_users --dry-run
    python manage.py create_endpoints_for_all_users --user-ids 1,5,12
    python manage.py create_endpoints_for_all_users --force
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.games.hf_inference import verify_model
from apps.users.models import UserGameModel


def _derive_endpoint_name(repo_id: str, game_type: str) -> str:
    """Derive a stable endpoint name from the repo owner and game type.

    Example: repo_id="alice/chess-model", game_type="chess"
             → "alice-chess"
    """
    owner = repo_id.split("/")[0] if "/" in repo_id else repo_id
    return f"{owner}-{game_type}"


class Command(BaseCommand):
    help = (
        "Bulk-verify models via HF API for UserGameModels "
        "that have a model repo but have not been verified yet. "
        "Also fills hf_inference_endpoint_name/id/status for all game types."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--game-type",
            type=str,
            choices=[gt for gt, _ in UserGameModel.GameType.choices],
            default="",
            help="Limit to a specific game type (default: all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List models that would be verified without running verification.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-verify models even if already approved.",
        )
        parser.add_argument(
            "--user-ids",
            type=str,
            default="",
            help="Comma-separated list of user IDs to process (e.g. '1,5,12'). "
                 "If omitted, all eligible users are processed.",
        )

    def handle(self, *args, **options):
        qs = UserGameModel.objects.filter(hf_model_repo_id__gt="")

        if options["game_type"]:
            qs = qs.filter(game_type=options["game_type"])

        if not options["force"]:
            qs = qs.exclude(verification_status="approved")

        # Filter to specific users if --user-ids was given
        user_ids_str = options.get("user_ids", "")
        if user_ids_str:
            try:
                user_ids = [int(x.strip()) for x in user_ids_str.split(",") if x.strip()]
            except ValueError:
                self.stderr.write(self.style.ERROR("Invalid --user-ids format. Use comma-separated integers."))
                return
            qs = qs.filter(user_id__in=user_ids)
            self.stdout.write(f"Filtering to user IDs: {user_ids}\n")

        models = list(qs.select_related("user"))

        if not models:
            self.stdout.write(self.style.WARNING("No eligible UserGameModels found."))
            return

        self.stdout.write(
            f"Found {len(models)} model(s) to process.\n"
        )

        verified = 0
        failed = 0

        for gm in models:
            label = f"{gm.user.username}/{gm.game_type} ({gm.hf_model_repo_id})"

            # Derive endpoint identifiers from the repo ID — no hardcoding.
            endpoint_name = _derive_endpoint_name(gm.hf_model_repo_id, gm.game_type)

            if options["dry_run"]:
                self.stdout.write(
                    f"  [DRY RUN] Would verify {label}\n"
                    f"            endpoint_name/id → {endpoint_name!r}"
                )
                continue

            # ── Run integrity verification ───────────────────────────────
            new_status = "pending"
            try:
                result = verify_model(gm, force=options["force"])
                ok, msg = (result[0], result[1]) if isinstance(result, tuple) else (bool(result), "")
                new_status = "ready" if ok else "failed"

                if ok:
                    self.stdout.write(self.style.SUCCESS(f"  ✔ {label} → approved"))
                    verified += 1
                else:
                    self.stderr.write(self.style.ERROR(f"  ✖ {label} → {msg}"))
                    failed += 1
            except Exception as exc:
                new_status = "failed"
                self.stderr.write(self.style.ERROR(f"  ✖ {label} — {exc}"))
                failed += 1

            # ── Fill endpoint fields (only overwrite blanks unless --force) ─
            update_fields: dict[str, str] = {}

            if not gm.hf_inference_endpoint_name or options["force"]:
                update_fields["hf_inference_endpoint_name"] = endpoint_name

            if not gm.hf_inference_endpoint_id or options["force"]:
                update_fields["hf_inference_endpoint_id"] = endpoint_name

            if not gm.hf_inference_endpoint_status or options["force"]:
                update_fields["hf_inference_endpoint_status"] = new_status

            if update_fields:
                with transaction.atomic():
                    UserGameModel.objects.filter(pk=gm.pk).update(**update_fields)
                self.stdout.write(
                    f"    saved endpoint fields: {update_fields}"
                )

        if options["dry_run"]:
            self.stdout.write(f"\nDry run complete. {len(models)} model(s) would be processed.")
        else:
            self.stdout.write(
                f"\nDone. Verified: {verified}, Failed: {failed}"
            )
