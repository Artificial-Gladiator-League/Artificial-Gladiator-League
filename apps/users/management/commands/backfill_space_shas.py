"""Backfill ``approved_space_sha`` for UserGameModel rows with a Space URL.

Problem
-------
``resolve_space_repo_id`` previously returned a malformed repo ID for the
Gradio subdomain form (``https://owner-name.hf.space``), so the Space SHA
never resolved and ``approved_space_sha`` was silently left empty. With the
parser now fixed, this command re-resolves and persists the Space SHA for
every affected row.

Scope: rows where ``hf_inference_endpoint_url`` is set but
``approved_space_sha`` is empty.

Usage
-----
    python manage.py backfill_space_shas
    python manage.py backfill_space_shas --dry-run
    python manage.py backfill_space_shas --user 143
    python manage.py backfill_space_shas --no-email

Mirrors apps/games/management/commands/pin_chess_model_shas.py.
"""
from __future__ import annotations

import logging

from django.core.mail import mail_admins
from django.core.management.base import BaseCommand
from django.db.models import Q

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Backfill approved_space_sha for UserGameModel rows by resolving the "
        "HF Space repo HEAD via the now-fixed resolve_space_repo_id parser."
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
            "--no-email",
            action="store_true",
            help="Skip the mail_admins() summary at the end.",
        )

    def handle(self, *args, **options):
        from apps.users.integrity import _get_stored_token, _resolve_ref_sha
        from apps.users.models import UserGameModel
        from apps.users.ownership_verification import resolve_space_repo_id

        dry_run: bool = options["dry_run"]
        only_user: int | None = options.get("user")
        send_email: bool = not options["no_email"]

        qs = (
            UserGameModel.objects
            .exclude(hf_inference_endpoint_url="")
            .filter(Q(approved_space_sha="") | Q(approved_space_sha__isnull=True))
            .select_related("user")
        )
        if only_user:
            qs = qs.filter(user_id=only_user)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "No rows to backfill (all Space SHAs already pinned)."
            ))
            return

        self.stdout.write(
            f"Found {total} UserGameModel row(s) with a Space URL to backfill"
            f"{' (dry-run)' if dry_run else ''}.\n"
        )

        fixed: list[tuple[str, str, str]] = []    # (username, repo, sha[:12])
        unreachable: list[tuple[str, str]] = []   # (username, repo)
        skipped: list[tuple[str, str, str]] = []  # (username, url, reason)

        for gm in qs.order_by("user_id"):
            username = gm.user.username
            space_url = (gm.hf_inference_endpoint_url or "").strip()
            space_repo_id = resolve_space_repo_id(space_url)
            if not space_repo_id:
                self.stdout.write(self.style.WARNING(
                    f"  [skip] user={username} url={space_url}: unparseable Space URL"
                ))
                skipped.append((username, space_url, "unparseable"))
                continue

            ref = (gm.submitted_ref or "main").strip() or "main"
            token = _get_stored_token(gm.user) or ""

            try:
                latest_sha = _resolve_ref_sha(
                    space_repo_id, token, ref=ref, repo_type="space",
                )
            except Exception as exc:
                log.exception("backfill_space_shas: resolve failed for %s", space_repo_id)
                self.stdout.write(self.style.ERROR(
                    f"  [err]  user={username} space={space_repo_id}: {exc}"
                ))
                unreachable.append((username, space_repo_id))
                continue

            if not latest_sha:
                self.stdout.write(self.style.ERROR(
                    f"  [miss] user={username} space={space_repo_id}@{ref}: HF returned no sha"
                ))
                unreachable.append((username, space_repo_id))
                continue

            self.stdout.write(self.style.SUCCESS(
                f"  [ok]   user={username} space={space_repo_id}@{ref} sha={latest_sha[:12]}"
            ))
            fixed.append((username, space_repo_id, latest_sha[:12]))

            if dry_run:
                continue

            gm.approved_space_sha = latest_sha
            try:
                gm.save(update_fields=["approved_space_sha"])
            except Exception:
                log.exception(
                    "backfill_space_shas: failed to persist for user=%s space=%s",
                    username, space_repo_id,
                )
                unreachable.append((username, space_repo_id))

        summary_lines = [
            f"backfill_space_shas summary "
            f"({'dry-run' if dry_run else 'applied'}):",
            f"  fixed:       {len(fixed)}",
            f"  unreachable: {len(unreachable)}",
            f"  skipped:     {len(skipped)}",
        ]
        if fixed:
            summary_lines.append("\nFixed:")
            summary_lines.extend(f"  - {u} {r} -> {s}" for (u, r, s) in fixed)
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
                        f"[AGL] Backfilled {len(fixed)} Space SHA(s) "
                        f"({len(unreachable)} unreachable)"
                    ),
                    message=body,
                )
            except Exception:
                log.exception("backfill_space_shas: mail_admins failed")
