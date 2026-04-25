"""Management command: populate_space_urls

Backfills hf_inference_endpoint_url for every UserGameModel row where
it is blank, using the HF Space URL convention:
    owner = repo_id.split("/")[0]
    url   = https://{owner}-{owner}.hf.space

Optionally probes each derived URL to confirm the Space is live and
sets hf_inference_endpoint_status = 'ready' | 'failed' accordingly.

Usage:
    python manage.py populate_space_urls
    python manage.py populate_space_urls --probe      # HTTP probe each URL
    python manage.py populate_space_urls --dry-run    # preview only
    python manage.py populate_space_urls --force      # overwrite existing URLs
    python manage.py populate_space_urls --user 56    # single user
"""
from __future__ import annotations

import logging

import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.models import UserGameModel

log = logging.getLogger(__name__)

_PROBE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
_PROBE_TIMEOUT = 20  # seconds


def _derive_space_url(repo_id: str) -> str:
    owner = repo_id.split("/")[0] if "/" in repo_id else repo_id
    return f"https://{owner}-{owner}.hf.space"


def _probe_url(url: str) -> tuple[bool, str]:
    """POST to the Gradio API and return (success, message)."""
    submit_url = f"{url}/gradio_api/call/get_move"
    try:
        resp = requests.post(
            submit_url,
            json={"data": [_PROBE_FEN]},
            headers={"Content-Type": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if resp.status_code >= 500:
            return False, f"HTTP {resp.status_code}"
        return True, f"HTTP {resp.status_code} — Space is reachable"
    except requests.exceptions.ConnectionError as exc:
        return False, f"connection error: {exc}"
    except requests.exceptions.Timeout:
        return False, f"timed out after {_PROBE_TIMEOUT}s"
    except requests.exceptions.RequestException as exc:
        return False, f"request failed: {exc}"


class Command(BaseCommand):
    help = (
        "Backfill hf_inference_endpoint_url for UserGameModel rows where it "
        "is blank. Derives URL from repo owner using HF Space naming convention."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--probe",
            action="store_true",
            help="HTTP-probe each derived URL and set status to 'ready' or 'failed'.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite hf_inference_endpoint_url even if already set.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing to the database.",
        )
        parser.add_argument(
            "--user",
            type=int,
            help="Only process models belonging to this user ID.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        probe: bool = options["probe"]
        force: bool = options["force"]

        qs = UserGameModel.objects.exclude(hf_model_repo_id="").select_related("user")
        if not force:
            qs = qs.filter(hf_inference_endpoint_url="")
        if options.get("user"):
            qs = qs.filter(user_id=options["user"])

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "Nothing to update — all records already have Space URLs "
                "(use --force to overwrite existing URLs)."
            ))
            return

        self.stdout.write(
            f"Found {total} record(s) to process"
            f"{' (dry-run)' if dry_run else ''}"
            f"{' + probe' if probe else ''}.\n"
        )

        ok = failed = skipped = 0

        for ugm in qs.order_by("user_id"):
            derived_url = _derive_space_url(ugm.hf_model_repo_id)
            self.stdout.write(
                f"  #{ugm.pk} user={ugm.user.username} "
                f"repo={ugm.hf_model_repo_id} → {derived_url}"
            )

            if dry_run:
                self.stdout.write("    [dry-run — skipped]")
                continue

            update_fields: dict[str, str] = {
                "hf_inference_endpoint_url": derived_url,
            }

            if probe:
                success, msg = _probe_url(derived_url)
                new_status = "ready" if success else "failed"
                update_fields["hf_inference_endpoint_status"] = new_status
                if success:
                    ok += 1
                    self.stdout.write(self.style.SUCCESS(f"    ✓ {msg}"))
                else:
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"    ✗ {msg}"))
            else:
                skipped += 1
                update_fields["hf_inference_endpoint_status"] = "pending"
                self.stdout.write("    (no probe — status → pending)")

            with transaction.atomic():
                UserGameModel.objects.filter(pk=ugm.pk).update(**update_fields)

        if not dry_run:
            parts = []
            if probe:
                parts.append(f"{ok} ready, {failed} failed")
            else:
                parts.append(f"{skipped} URL(s) set to pending (run --probe to validate)")
            self.stdout.write(self.style.SUCCESS(f"\nDone — {', '.join(parts)}."))
            if probe and failed:
                self.stdout.write(
                    self.style.WARNING(
                        "\nFailed spaces may not be deployed yet or use a different URL slug.\n"
                        "Run `python manage.py verify_chess_models --force` after the Space "
                        "is confirmed running to update status."
                    )
                )
