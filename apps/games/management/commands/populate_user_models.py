from __future__ import annotations

import logging
from typing import List

from django.core.management.base import BaseCommand

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Populate per-user model/data folders from local HF cache (no network)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-ids",
            nargs="+",
            type=int,
            required=True,
            help="Space-separated list of user ids to populate",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Remove existing user folders before populating",
        )

    def handle(self, *args, **options):
        user_ids: List[int] = options.get("user_ids") or []
        force: bool = options.get("force", False)

        from apps.games.model_preloader import clear_user_models, populate_from_config

        for uid in user_ids:
            try:
                self.stdout.write(f"Populating user {uid}...")
                if force:
                    try:
                        clear_user_models(uid)
                    except Exception:
                        log.exception("Failed to clear user models for %s", uid)
                for game in ("chess", "breakthrough"):
                    try:
                        populate_from_config(uid, game)
                        self.stdout.write(f"  OK: {game}")
                    except Exception:
                        log.exception("populate_from_config failed for user %s game %s", uid, game)
                        self.stdout.write(self.style.ERROR(f"  FAILED: {game} (see logs)"))
            except Exception:
                log.exception("populate_user_models command failed for user %s", uid)
                self.stdout.write(self.style.ERROR(f"User {uid} failed"))
