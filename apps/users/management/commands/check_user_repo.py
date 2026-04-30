import logging

from django.core.management.base import BaseCommand
from django.db import transaction

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Show resolved repo from bot_runner._get_repo_for_user for a given "
        "user. With --auto-disqualify, also DQ the user from every ONGOING "
        "tournament for SHA-mismatch (used when a repo change is detected "
        "out-of-band)."
    )

    def add_arguments(self, parser):
        parser.add_argument('--user-id', type=int, required=True)
        parser.add_argument('--game-type', type=str, default='breakthrough')
        parser.add_argument(
            '--auto-disqualify',
            action='store_true',
            help=(
                "Disqualify the user from all ongoing tournaments with "
                "reason='Repo changed during tournament'."
            ),
        )

    def handle(self, *args, **options):
        user_id = options['user_id']
        game_type = options['game_type']
        auto_dq = options['auto_disqualify']

        try:
            from apps.users.models import CustomUser
            from apps.games.bot_runner import _get_repo_for_user
        except Exception as exc:
            self.stderr.write(f"Import error: {exc}")
            return

        try:
            user = CustomUser.objects.get(pk=user_id)
        except CustomUser.DoesNotExist:
            self.stderr.write(f"User id={user_id} not found")
            return

        repo = _get_repo_for_user(user, game_type)
        self.stdout.write(
            f"Resolved repo for user {user.username} (id={user_id}) "
            f"game_type={game_type}: {repr(repo)}"
        )

        if not auto_dq:
            return

        # ── Auto-disqualify path ────────────────────────────────
        from apps.tournaments.models import Tournament, TournamentParticipant

        reason = "Repo changed during tournament"

        participants = list(
            TournamentParticipant.objects
            .filter(
                user=user,
                tournament__status=Tournament.Status.ONGOING,
            )
            .select_related("tournament")
        )

        if not participants:
            self.stdout.write(
                f"No ongoing tournaments to disqualify {user.username} from."
            )
            return

        # Prefer the canonical service so QA-lobby removal + live-match
        # forfeit + middleware redirect all kick in identically to the
        # automatic anti-cheat audit path.
        try:
            from apps.tournaments.disqualification import (
                disqualify_for_repo_change,
            )
        except Exception:
            disqualify_for_repo_change = None

        for p in participants:
            with transaction.atomic():
                if disqualify_for_repo_change is not None:
                    try:
                        disqualify_for_repo_change(
                            p, reason=reason, forfeit_live_match=True,
                        )
                    except Exception:
                        log.exception(
                            "disqualify_for_repo_change failed for "
                            "participant=%s — falling back to manual flip",
                            p.pk,
                        )
                        p.disqualified_for_sha_mismatch = True
                        if hasattr(p, "disqualified_reason"):
                            p.disqualified_reason = reason
                        p.save(update_fields=[
                            f for f in (
                                "disqualified_for_sha_mismatch",
                                "disqualified_reason",
                            ) if hasattr(p, f)
                        ])
                else:
                    p.disqualified_for_sha_mismatch = True
                    if hasattr(p, "disqualified_reason"):
                        p.disqualified_reason = reason
                    p.save(update_fields=[
                        f for f in (
                            "disqualified_for_sha_mismatch",
                            "disqualified_reason",
                        ) if hasattr(p, f)
                    ])

            msg = (
                f"\U0001F6A8 TERMINAL: DISQUALIFIED {user.username} from "
                f"{p.tournament.name} (Round {p.tournament.current_round})"
            )
            try:
                self.stdout.write(msg)
            except UnicodeEncodeError:
                self.stdout.write(
                    msg.encode("ascii", "replace").decode("ascii")
                )
            log.warning(
                "check_user_repo --auto-disqualify: user=%s tournament=%s "
                "round=%s reason=%r",
                user.username, p.tournament_id,
                p.tournament.current_round, reason,
            )

        self.stdout.write(
            f"Disqualified {user.username} from {len(participants)} "
            f"ongoing tournament(s)."
        )
