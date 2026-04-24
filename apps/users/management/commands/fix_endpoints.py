"""Management command: fix_endpoints

Bulk-fixes UserGameModel records whose hf_inference_endpoint_id is null or
blank, which causes IntegrityError on MySQL NOT NULL columns.

Usage:
    python manage.py fix_endpoints [--dry-run]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.models import UserGameModel


def _derive_endpoint_id(ugm: UserGameModel) -> str:
    """Build a deterministic endpoint ID from username + repo slug."""
    username = ugm.user.username
    repo_slug = ugm.hf_model_repo_id.split("/")[-1] if ugm.hf_model_repo_id else "model"
    return f"{username}-{repo_slug}"


def _derive_endpoint_name(ugm: UserGameModel) -> str:
    """Use the repo slug as the endpoint name."""
    return ugm.hf_model_repo_id.split("/")[-1] if ugm.hf_model_repo_id else "model"


class Command(BaseCommand):
    help = (
        "Populate hf_inference_endpoint_id (and name/status) for every "
        "UserGameModel row where it is null or blank."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]

        qs = UserGameModel.objects.filter(
            hf_inference_endpoint_id__isnull=True
        ) | UserGameModel.objects.filter(hf_inference_endpoint_id="")

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to fix — all records already have endpoint IDs."))
            return

        self.stdout.write(f"Found {total} record(s) to fix{' (dry-run)' if dry_run else ''}.")

        updated = 0
        with transaction.atomic():
            for ugm in qs.select_related("user"):
                new_id = _derive_endpoint_id(ugm)
                new_name = _derive_endpoint_name(ugm)
                self.stdout.write(
                    f"  #{ugm.pk} user={ugm.user.username} repo={ugm.hf_model_repo_id} "
                    f"→ endpoint_id={new_id!r}"
                )
                if not dry_run:
                    ugm.hf_inference_endpoint_id = new_id
                    if not ugm.hf_inference_endpoint_name:
                        ugm.hf_inference_endpoint_name = new_name
                    if not ugm.hf_inference_endpoint_status:
                        ugm.hf_inference_endpoint_status = "pending"
                    ugm.save(update_fields=[
                        "hf_inference_endpoint_id",
                        "hf_inference_endpoint_name",
                        "hf_inference_endpoint_status",
                    ])
                    updated += 1

            if dry_run:
                transaction.set_rollback(True)

        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry-run complete — {total} record(s) would be updated."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Done — {updated} record(s) updated."))
