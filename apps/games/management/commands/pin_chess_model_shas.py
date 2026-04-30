"""Backfill ``approved_full_sha`` for legacy chess UserGameModel rows.

Problem
-------
Some legacy chess players have ``approved_full_sha = ""`` (or None) even
though they have completed 30+ rated games and are otherwise fully
verified. This causes ``apps.users.integrity.live_sha_check`` to log
``current_sha=None`` and any tournament join goes through the
"no baseline yet" branch — which combined with stricter gates ends up
silently blocking these accounts.

This command resolves each affected repo's latest commit SHA via
``huggingface_hub`` and writes back the three SHA fields, marks the
model as integrity-OK, and ensures the rated-game counter is at least
``REVALIDATION_GAMES_REQUIRED`` so the user can immediately rejoin
tournaments.

Usage
-----
    python manage.py pin_chess_model_shas
    python manage.py pin_chess_model_shas --dry-run
    python manage.py pin_chess_model_shas --user 143
    python manage.py pin_chess_model_shas --include-unverified
    python manage.py pin_chess_model_shas --no-email

The default scope is: ``game_type='chess'``, ``is_verified=True``,
``rated_games_played >= REVALIDATION_GAMES_REQUIRED`` and an empty
``approved_full_sha``.
"""
from __future__ import annotations

import logging
from typing import Iterable

from django.core.mail import mail_admins
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Backfill approved_full_sha for legacy chess UserGameModel rows by "
        "resolving the current HF repo HEAD and persisting the SHA."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve SHAs and report, but do not write any changes.",
        )
        parser.add_argument(
            "--user",
            type=int,
            help="Only process the given user id.",
        )
        parser.add_argument(
            "--include-unverified",
            action="store_true",
            help="Also process rows where is_verified=False.",
        )
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Skip the mail_admins() summary at the end.",
        )

    # ── main ──────────────────────────────────────────────
    def handle(self, *args, **options):
        from apps.users.integrity import (
            REVALIDATION_GAMES_REQUIRED,
            _get_stored_token,
            _resolve_ref_sha,
        )
        from apps.users.models import UserGameModel

        dry_run: bool = options["dry_run"]
        only_user: int | None = options.get("user")
        include_unverified: bool = options["include_unverified"]
        send_email: bool = not options["no_email"]

        # Build queryset: rows missing approved_full_sha that are otherwise
        # eligible (verified + 30+ rated games). Empty string and NULL both
        # count as "missing".
        qs = (
            UserGameModel.objects
            .filter(game_type="chess")
            .filter(Q(approved_full_sha="") | Q(approved_full_sha__isnull=True))
            .exclude(hf_model_repo_id="")
            .select_related("user")
        )
        if not include_unverified:
            qs = qs.filter(is_verified=True)
        if only_user:
            qs = qs.filter(user_id=only_user)
        else:
            qs = qs.filter(rated_games_played__gte=REVALIDATION_GAMES_REQUIRED)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "No legacy rows to backfill (all chess models already pinned)."
            ))
            return

        self.stdout.write(
            f"Found {total} chess UserGameModel row(s) to backfill"
            f"{' (dry-run)' if dry_run else ''}.\n"
        )

        fixed: list[tuple[str, str, str]] = []   # (username, repo, sha[:12])
        unreachable: list[tuple[str, str]] = []  # (username, repo)
        skipped: list[tuple[str, str, str]] = [] # (username, repo, reason)

        for gm in qs.order_by("user_id"):
            username = gm.user.username
            repo_id = (gm.hf_model_repo_id or "").strip()
            ref = (gm.submitted_ref or "main").strip() or "main"
            repo_type = (gm.submission_repo_type or "model").strip() or "model"

            token = _get_stored_token(gm.user) or ""
            if not token:
                self.stdout.write(self.style.WARNING(
                    f"  [skip] user={username} repo={repo_id}: no HF token available"
                ))
                skipped.append((username, repo_id, "no token"))
                continue

            try:
                latest_sha = _resolve_ref_sha(
                    repo_id, token, ref=ref, repo_type=repo_type,
                )
            except Exception as exc:  # defensive — _resolve_ref_sha already swallows
                log.exception("pin_chess_model_shas: resolve failed for %s", repo_id)
                self.stdout.write(self.style.ERROR(
                    f"  [err]  user={username} repo={repo_id}: {exc}"
                ))
                unreachable.append((username, repo_id))
                continue

            if not latest_sha:
                self.stdout.write(self.style.ERROR(
                    f"  [miss] user={username} repo={repo_id}@{ref}: HF returned no sha"
                ))
                unreachable.append((username, repo_id))
                continue

            target_counter = max(
                gm.rated_games_since_revalidation, REVALIDATION_GAMES_REQUIRED,
            )
            self.stdout.write(self.style.SUCCESS(
                f"  [ok]   user={username} repo={repo_id}@{ref} "
                f"sha={latest_sha[:12]} counter={target_counter}"
            ))
            fixed.append((username, repo_id, latest_sha[:12]))

            if dry_run:
                continue

            now = timezone.now()
            gm.approved_full_sha = latest_sha
            gm.original_model_commit_sha = (
                gm.original_model_commit_sha or latest_sha
            )
            gm.last_known_commit_id = latest_sha
            gm.model_integrity_ok = True
            gm.rated_games_since_revalidation = target_counter
            gm.last_model_validation_date = now.date()
            if not gm.pinned_at:
                gm.pinned_at = now
            try:
                gm.save(update_fields=[
                    "approved_full_sha",
                    "original_model_commit_sha",
                    "last_known_commit_id",
                    "model_integrity_ok",
                    "rated_games_since_revalidation",
                    "last_model_validation_date",
                    "pinned_at",
                ])
            except Exception:
                log.exception(
                    "pin_chess_model_shas: failed to persist for user=%s repo=%s",
                    username, repo_id,
                )
                unreachable.append((username, repo_id))

        # ── summary ────────────────────────────────────────
        summary_lines = [
            f"pin_chess_model_shas summary "
            f"({'dry-run' if dry_run else 'applied'}):",
            f"  fixed:       {len(fixed)}",
            f"  unreachable: {len(unreachable)}",
            f"  skipped:     {len(skipped)}",
        ]
        if fixed:
            summary_lines.append("\nFixed:")
            summary_lines.extend(
                f"  - {u} {r} -> {s}" for (u, r, s) in fixed
            )
        if unreachable:
            summary_lines.append("\nUnreachable:")
            summary_lines.extend(f"  - {u} {r}" for (u, r) in unreachable)
        if skipped:
            summary_lines.append("\nSkipped:")
            summary_lines.extend(f"  - {u} {r} ({why})" for (u, r, why) in skipped)

        body = "\n".join(summary_lines)
        self.stdout.write("\n" + body)

        if send_email and not dry_run and (fixed or unreachable):
            try:
                mail_admins(
                    subject=(
                        f"[AGL] Pinned {len(fixed)} chess model SHA(s) "
                        f"({len(unreachable)} unreachable)"
                    ),
                    message=body,
                )
            except Exception:
                log.exception("pin_chess_model_shas: mail_admins failed")

    # Expose the iterable for re-use in tests / other commands.
    @staticmethod
    def candidate_queryset(include_unverified: bool = False) -> Iterable:
        from apps.users.integrity import REVALIDATION_GAMES_REQUIRED
        from apps.users.models import UserGameModel

        qs = (
            UserGameModel.objects
            .filter(game_type="chess")
            .filter(Q(approved_full_sha="") | Q(approved_full_sha__isnull=True))
            .filter(rated_games_played__gte=REVALIDATION_GAMES_REQUIRED)
            .exclude(hf_model_repo_id="")
        )
        if not include_unverified:
            qs = qs.filter(is_verified=True)
        return qs
