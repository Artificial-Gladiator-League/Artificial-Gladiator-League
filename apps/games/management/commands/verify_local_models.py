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

    def handle(self, *args, **options):
        from apps.users.models import UserGameModel
        from apps.games.local_inference import verify_local_files, resolve_model_path
        from apps.games.model_check import check_model
        from apps.games.exceptions import SandboxUnavailableError

        user_id: int | None = options["user_id"]
        game_type: str | None = options["game_type"]

        qs = UserGameModel.objects.select_related("user").all()
        if user_id is not None:
            qs = qs.filter(user_id=user_id)
        if game_type is not None:
            qs = qs.filter(game_type=game_type)

        records = list(qs.order_by("user_id", "game_type"))
        if not records:
            self.stdout.write(self.style.WARNING("No UserGameModel records matched the filters."))
            return

        total = passed = failed = unavailable = 0

        for gm in records:
            uid = gm.user_id
            gtype = gm.game_type
            label = f"user={uid} game={gtype}"
            total += 1

            # ── Check files exist ───────────────────────────────────────────
            ok, msg = verify_local_files(gm)
            if not ok:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  MISSING  {label}: {msg}"))
                continue

            self.stdout.write(f"  FILES OK  {label}: {msg}")

            # ── Contract check (only a passing model may become ACTIVE) ────
            model_dir, data_dir = resolve_model_path(uid, gtype, repo_id=gm.hf_model_repo_id or None)
            try:
                problems = check_model(model_dir, data_dir, gtype) if model_dir else [msg]
            except SandboxUnavailableError as exc:
                unavailable += 1
                self.stderr.write(self.style.WARNING(f"  SANDBOX UNAVAILABLE  {label}: {exc}"))
                continue

            if problems:
                gm.status = gm.ContractStatus.FAILED
                gm.last_error = "; ".join(problems)
                gm.save(update_fields=["status", "last_error"])
                failed += 1
                self.stderr.write(self.style.ERROR(f"  CONTRACT FAIL  {label}: {gm.last_error}"))
                continue

            gm.status = gm.ContractStatus.ACTIVE
            gm.last_error = ""
            gm.save(update_fields=["status", "last_error"])
            passed += 1
            self.stdout.write(self.style.SUCCESS(f"  CONTRACT OK  {label}"))

        self.stdout.write("")
        self.stdout.write(
            f"Results: {total} model(s) checked — "
            f"{self.style.SUCCESS(str(passed) + ' passed')}, "
            f"{self.style.ERROR(str(failed) + ' failed')}, "
            f"{self.style.WARNING(str(unavailable) + ' sandbox-unavailable')}"
        )

        if failed:
            sys.exit(1)
