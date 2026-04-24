# ──────────────────────────────────────────────
# apps/games/management/commands/verify_local_models.py
#
# Management command to verify committed local model files.
#
# Usage examples
# ──────────────
# Verify all models, print results:
#   python manage.py verify_local_models
#
# Verify a specific user + game:
#   python manage.py verify_local_models --user-id 133 --game-type chess
#
# Record integrity baseline for all models (run once at submission):
#   python manage.py verify_local_models --record-baseline
#
# Check integrity against stored baselines (pre-tournament gate):
#   python manage.py verify_local_models --check-integrity
# ──────────────────────────────────────────────
from __future__ import annotations

import sys
import logging

from django.core.management.base import BaseCommand, CommandError

log = logging.getLogger(__name__)

VALID_GAME_TYPES = ("chess", "breakthrough")


class Command(BaseCommand):
    help = "Verify committed local model files for all or specific users."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-id",
            type=int,
            default=None,
            help="Only process models for this user ID.",
        )
        parser.add_argument(
            "--game-type",
            type=str,
            choices=VALID_GAME_TYPES,
            default=None,
            help="Only process this game type (chess or breakthrough).",
        )
        parser.add_argument(
            "--record-baseline",
            action="store_true",
            default=False,
            help="Record a fresh integrity baseline for each model found.",
        )
        parser.add_argument(
            "--check-integrity",
            action="store_true",
            default=False,
            help="Compare current file hashes against stored baseline.",
        )

    def handle(self, *args, **options):
        from apps.users.models import UserGameModel
        from apps.games.local_inference import verify_local_files
        from apps.users.integrity import record_local_baseline, check_local_integrity

        user_id: int | None = options["user_id"]
        game_type: str | None = options["game_type"]
        do_baseline: bool = options["record_baseline"]
        do_integrity: bool = options["check_integrity"]

        qs = UserGameModel.objects.select_related("user").all()
        if user_id is not None:
            qs = qs.filter(user_id=user_id)
        if game_type is not None:
            qs = qs.filter(game_type=game_type)

        records = list(qs.order_by("user_id", "game_type"))
        if not records:
            self.stdout.write(self.style.WARNING("No UserGameModel records matched the filters."))
            return

        total = passed = failed = 0

        for gm in records:
            uid = gm.user_id
            gtype = gm.game_type
            label = f"user={uid} game={gtype}"
            total += 1

            # ── 1. Check files exist ────────────────────────────────────────
            ok, msg = verify_local_files(gm)
            if not ok:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  MISSING  {label}: {msg}"))
                continue

            self.stdout.write(f"  FILES OK  {label}: {msg}")

            # ── 2. Record baseline ─────────────────────────────────────────
            if do_baseline:
                b_ok, b_msg = record_local_baseline(gm)
                if b_ok:
                    self.stdout.write(self.style.SUCCESS(f"  BASELINE  {label}: {b_msg}"))
                else:
                    self.stderr.write(self.style.ERROR(f"  BASELINE FAIL  {label}: {b_msg}"))
                    failed += 1
                    continue

            # ── 3. Check integrity against baseline ────────────────────────
            if do_integrity:
                i_ok, i_msg = check_local_integrity(gm, alert_admins=False)
                if i_ok:
                    self.stdout.write(self.style.SUCCESS(f"  INTEGRITY OK  {label}: {i_msg}"))
                    passed += 1
                else:
                    self.stderr.write(self.style.ERROR(f"  INTEGRITY FAIL  {label}: {i_msg}"))
                    failed += 1
                    continue

            passed += 1

        self.stdout.write("")
        self.stdout.write(
            f"Results: {total} model(s) checked — "
            f"{self.style.SUCCESS(str(passed) + ' passed')}, "
            f"{self.style.ERROR(str(failed) + ' failed')}"
        )

        if failed:
            sys.exit(1)
