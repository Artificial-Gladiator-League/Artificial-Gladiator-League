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

        from apps.games.model_preloader import (
            clear_user_models,
            ensure_hf_snapshot,
            populate_from_config,
        )
        from apps.users.models import UserGameModel
        from django.utils import timezone

        for uid in user_ids:
            try:
                self.stdout.write(f"Populating user {uid}...")
                if force:
                    try:
                        clear_user_models(uid)
                    except Exception:
                        log.exception("Failed to clear user models for %s", uid)

                # ── Mirror each registered HF repo into the shared HF hub cache.
                # This guarantees `_find_hf_cache_snapshot()` resolves locally,
                # so model_preloader.preload_user_models stops warning and
                # inference does not fall back to the slow HF API path.
                try:
                    ugms = list(
                        UserGameModel.objects
                        .filter(user_id=uid, game_type__in=("chess", "breakthrough"))
                        .exclude(hf_model_repo_id="")
                        .select_related("user")
                    )
                except Exception:
                    log.exception("Could not fetch UserGameModel rows for user %s", uid)
                    ugms = []

                for gm in ugms:
                    repo_id = (gm.hf_model_repo_id or "").strip()
                    if not repo_id:
                        continue
                    self.stdout.write(f"  Mirroring repo {repo_id} ({gm.game_type})...")
                    snap = ensure_hf_snapshot(gm)
                    if snap is None:
                        self.stdout.write(self.style.WARNING(
                            f"    no snapshot mirrored for {repo_id} (see logs)"
                        ))
                        continue
                    sha = snap.name
                    self.stdout.write(self.style.SUCCESS(
                        f"    cached snapshot at {snap} (sha={sha})"
                    ))
                    # Persist cache metadata onto UserGameModel so
                    # downstream code (verify_chess_models, preloader,
                    # predict_*) can rely on cached_at / cached_commit.
                    update_fields: list[str] = []
                    snap_str = str(snap)
                    if gm.cached_path != snap_str:
                        gm.cached_path = snap_str
                        update_fields.append("cached_path")
                    if sha and gm.cached_commit != sha:
                        gm.cached_commit = sha
                        update_fields.append("cached_commit")
                    gm.cached_at = timezone.now()
                    update_fields.append("cached_at")
                    if not gm.model_integrity_ok:
                        gm.model_integrity_ok = True
                        update_fields.append("model_integrity_ok")
                    try:
                        gm.save(update_fields=update_fields)
                    except Exception:
                        log.exception(
                            "Failed to persist cache metadata for user=%s repo=%s",
                            uid, repo_id,
                        )

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
