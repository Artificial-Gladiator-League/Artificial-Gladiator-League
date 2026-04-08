# ──────────────────────────────────────────────
# Management command: run_gauntlet
#
# Creates and runs a weekly "Gladiator Gauntlet"
# Swiss-system tournament with the top 8–16
# Elo-rated AI agents.
#
# Usage:
#   python manage.py run_gauntlet
#   python manage.py run_gauntlet --participants 12 --rounds 5
#   python manage.py run_gauntlet --dry-run
#
# Schedule via cron or Celery Beat (every Sunday 20:00 UTC).
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import math
import random
import time
import threading
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.games.models import Game
from apps.tournaments.models import (
    Badge,
    GauntletStanding,
    Match,
    Tournament,
    TournamentParticipant,
)
from apps.users.models import CustomUser

log = logging.getLogger(__name__)

# ── Default settings ─────────────────────────
DEFAULT_PARTICIPANTS = 16      # max players pulled from leaderboard
MIN_PARTICIPANTS = 4           # abort if fewer eligible AIs
DEFAULT_ROUNDS = 5
TIME_CONTROL = "3+1"
MOVE_DELAY = 0.3               # seconds between moves (for WS spectators)


class Command(BaseCommand):
    help = "Run a Gladiator Gauntlet — weekly automated Swiss tournament."

    def add_arguments(self, parser):
        parser.add_argument(
            "--participants", type=int, default=DEFAULT_PARTICIPANTS,
            help=f"Max participants (default {DEFAULT_PARTICIPANTS}).",
        )
        parser.add_argument(
            "--rounds", type=int, default=DEFAULT_ROUNDS,
            help=f"Number of Swiss rounds (default {DEFAULT_ROUNDS}).",
        )
        parser.add_argument(
            "--time-control", type=str, default=TIME_CONTROL,
            help=f"Time control string (default '{TIME_CONTROL}').",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print plan without running.",
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Main entry point
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def handle(self, *args, **options):
        max_p = options["participants"]
        rounds = options["rounds"]
        tc = options["time_control"]
        dry_run = options["dry_run"]

        self.stdout.write(self.style.HTTP_INFO(
            f"\n╔══════════════════════════════════════╗\n"
            f"║   GLADIATOR GAUNTLET — Swiss System  ║\n"
            f"╚══════════════════════════════════════╝\n"
        ))

        # ── 1. Select top Elo-rated AIs ───────────
        eligible = list(
            CustomUser.objects
            .filter(is_active=True, hf_model_repo_id__isnull=False)
            .exclude(hf_model_repo_id="")
            .order_by("-elo")[:max_p]
        )

        if len(eligible) < MIN_PARTICIPANTS:
            self.stdout.write(self.style.ERROR(
                f"Only {len(eligible)} eligible AIs (need ≥ {MIN_PARTICIPANTS}). Aborting."
            ))
            return

        self.stdout.write(f"  Participants: {len(eligible)}")
        for i, u in enumerate(eligible, 1):
            self.stdout.write(f"    {i:>2}. {u.username:<20} Elo {u.elo}")

        if dry_run:
            self.stdout.write(self.style.NOTICE("\n  DRY-RUN — nothing created."))
            return

        # ── 2. Determine week number ─────────────
        last_gauntlet = (
            Tournament.objects
            .filter(type=Tournament.Type.GAUNTLET)
            .order_by("-week_number")
            .first()
        )
        week_num = (last_gauntlet.week_number or 0) + 1 if last_gauntlet else 1

        # ── 3. Create tournament ─────────────────
        tournament = Tournament.objects.create(
            name=f"Gladiator Gauntlet — Week {week_num}",
            description=(
                f"Automated weekly Swiss tournament. "
                f"Top {len(eligible)} AIs compete over {rounds} rounds."
            ),
            type=Tournament.Type.GAUNTLET,
            format="swiss",
            capacity=len(eligible),
            rounds_total=rounds,
            time_control=tc,
            status=Tournament.Status.ONGOING,
            week_number=week_num,
            start_time=timezone.now(),
        )

        # ── 4. Register participants ─────────────
        participants = []
        standings = []
        for i, user in enumerate(eligible):
            participants.append(
                TournamentParticipant(
                    tournament=tournament, user=user, seed=i,
                )
            )
            standings.append(
                GauntletStanding(
                    tournament=tournament, user=user,
                )
            )
        TournamentParticipant.objects.bulk_create(participants)
        GauntletStanding.objects.bulk_create(standings)

        self.stdout.write(self.style.SUCCESS(
            f"\n  Created: {tournament.name} (pk={tournament.pk})"
        ))

        # ── 5. Run Swiss rounds ──────────────────
        players = list(eligible)  # mutable copy
        played_pairs: set[frozenset] = set()  # track who already played whom

        for round_num in range(1, rounds + 1):
            self.stdout.write(self.style.HTTP_INFO(
                f"\n  ── Round {round_num}/{rounds} ──"
            ))

            tournament.current_round = round_num
            tournament.save(update_fields=["current_round"])

            # Swiss pairing
            pairings = self._swiss_pair(tournament, players, played_pairs)

            if not pairings:
                self.stdout.write(self.style.WARNING(
                    "  No valid pairings possible — ending early."
                ))
                break

            # Create matches and run games
            for white, black in pairings:
                played_pairs.add(frozenset([white.pk, black.pk]))
                self._run_match(tournament, round_num, white, black, tc)

            # Wait for all games in this round to finish
            self._wait_for_round(tournament, round_num)

            # Update standings after the round
            self._update_standings(tournament)
            self._print_standings(tournament)

            # Broadcast standings to WS
            self._broadcast_standings(tournament, round_num)

        # ── 6. Finalize tournament ───────────────
        self._finalize_gauntlet(tournament)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Swiss pairing
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _swiss_pair(
        self,
        tournament: Tournament,
        players: list[CustomUser],
        played_pairs: set[frozenset],
    ) -> list[tuple[CustomUser, CustomUser]]:
        """Pair players by score + Elo proximity, avoiding repeat matchups.

        Algorithm:
        1. Sort players by (score DESC, elo DESC).
        2. Greedily pair from top down: for each unpaired player, find
           the next unpaired player they haven't played yet.
        3. Randomise colours for each pair.
        """
        standings = {
            s.user_id: s
            for s in tournament.standings.all()
        }

        sorted_players = sorted(
            players,
            key=lambda u: (
                -(standings[u.pk].score if u.pk in standings else 0),
                -u.elo,
            ),
        )

        paired = set()
        pairings: list[tuple[CustomUser, CustomUser]] = []

        for i, p1 in enumerate(sorted_players):
            if p1.pk in paired:
                continue
            for p2 in sorted_players[i + 1:]:
                if p2.pk in paired:
                    continue
                if frozenset([p1.pk, p2.pk]) in played_pairs:
                    continue
                # Valid pair found
                paired.add(p1.pk)
                paired.add(p2.pk)
                # Random colour
                if random.random() < 0.5:
                    pairings.append((p1, p2))
                else:
                    pairings.append((p2, p1))
                break

        # If a player is left unpaired (odd count) → they get a bye
        for p in sorted_players:
            if p.pk not in paired:
                self.stdout.write(f"    BYE: {p.username}")
                self._record_bye(tournament, p)

        return pairings

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Match execution
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _run_match(
        self,
        tournament: Tournament,
        round_num: int,
        white: CustomUser,
        black: CustomUser,
        tc: str,
    ) -> Match:
        """Create a Match + Game and launch the bot runner in a thread."""
        from apps.games.bot_runner import run_bot_game

        match = Match.objects.create(
            tournament=tournament,
            round_num=round_num,
            bracket_position=0,
            player1=white,
            player2=black,
            match_status=Match.MatchStatus.LIVE,
            time_control=tc,
        )

        parts = tc.split("+")
        base_sec = int(parts[0]) * 60 if parts else 180
        inc = int(parts[1]) if len(parts) > 1 else 0

        game = Game.objects.create(
            white=white,
            black=black,
            time_control=tc,
            white_time=float(base_sec),
            black_time=float(base_sec),
            increment=inc,
            status=Game.Status.ONGOING,
            is_tournament_game=True,
            tournament_match=match,
        )

        self.stdout.write(
            f"    Match: {white.username} (W) vs {black.username} (B) "
            f"[Game #{game.pk}]"
        )

        # Launch bot runner in a background thread
        t = threading.Thread(target=run_bot_game, args=(game.pk,), daemon=True)
        t.start()

        return match

    def _record_bye(self, tournament: Tournament, user: CustomUser) -> None:
        """Award a bye (1 point) to an unpaired player."""
        standing = GauntletStanding.objects.get(
            tournament=tournament, user=user,
        )
        standing.score += 1.0
        standing.wins += 1
        standing.save(update_fields=["score", "wins"])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Wait for round completion
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _wait_for_round(self, tournament: Tournament, round_num: int) -> None:
        """Poll until every match in the round is completed."""
        max_wait = 600  # 10 min safety limit
        elapsed = 0
        while elapsed < max_wait:
            pending = tournament.matches.filter(
                round_num=round_num,
                match_status__in=[Match.MatchStatus.PENDING, Match.MatchStatus.LIVE],
            ).count()
            if pending == 0:
                return
            time.sleep(2)
            elapsed += 2

        # Force-complete stuck matches
        stuck = tournament.matches.filter(
            round_num=round_num,
        ).exclude(match_status=Match.MatchStatus.COMPLETED)
        for m in stuck:
            m.result = "1/2-1/2"
            m.match_status = Match.MatchStatus.COMPLETED
            m.save(update_fields=["result", "match_status"])
            self.stdout.write(self.style.WARNING(
                f"    Force-drew stuck match: {m}"
            ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Standings update
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    @transaction.atomic
    def _update_standings(self, tournament: Tournament) -> None:
        """Recompute score, W/D/L, and Buchholz for every participant."""
        standings = {s.user_id: s for s in tournament.standings.all()}
        matches = tournament.matches.filter(
            match_status=Match.MatchStatus.COMPLETED,
            is_armageddon=False,
        ).select_related("player1", "player2", "winner")

        # Reset counts
        for s in standings.values():
            s.score = 0.0
            s.wins = 0
            s.draws = 0
            s.losses = 0

        # Replay all match results
        for m in matches:
            p1_s = standings.get(m.player1_id)
            p2_s = standings.get(m.player2_id)
            if not p1_s or not p2_s:
                continue

            if m.result == "1-0":
                p1_s.score += 1.0
                p1_s.wins += 1
                p2_s.losses += 1
            elif m.result == "0-1":
                p2_s.score += 1.0
                p2_s.wins += 1
                p1_s.losses += 1
            elif m.result == "1/2-1/2":
                p1_s.score += 0.5
                p1_s.draws += 1
                p2_s.score += 0.5
                p2_s.draws += 1

        # Buchholz tiebreak: sum of opponents' scores
        # Build opponent mapping first
        opponents: dict[int, list[int]] = {uid: [] for uid in standings}
        for m in matches:
            opponents.setdefault(m.player1_id, []).append(m.player2_id)
            opponents.setdefault(m.player2_id, []).append(m.player1_id)

        for uid, s in standings.items():
            s.buchholz = sum(
                standings[opp].score
                for opp in opponents.get(uid, [])
                if opp in standings
            )

        # Rank by score → buchholz → wins
        ranked = sorted(
            standings.values(),
            key=lambda s: (-s.score, -s.buchholz, -s.wins),
        )
        for i, s in enumerate(ranked, 1):
            s.rank = i

        GauntletStanding.objects.bulk_update(
            list(standings.values()),
            ["score", "wins", "draws", "losses", "buchholz", "rank"],
        )

    def _print_standings(self, tournament: Tournament) -> None:
        standings = tournament.standings.select_related("user").order_by("rank")
        self.stdout.write("\n    Rank  Player               Score  W  D  L  Buchholz")
        self.stdout.write("    " + "─" * 60)
        for s in standings:
            self.stdout.write(
                f"    {s.rank:>4}  {s.user.username:<20} {s.score:>5.1f}  "
                f"{s.wins:>1}  {s.draws:>1}  {s.losses:>1}  {s.buchholz:>8.1f}"
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Finalization — badges, announcement, HoF
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    @transaction.atomic
    def _finalize_gauntlet(self, tournament: Tournament) -> None:
        """Award badges, set champion, build announcement, mark completed."""
        standings = list(
            tournament.standings
            .select_related("user")
            .order_by("rank")
        )

        if not standings:
            tournament.status = Tournament.Status.COMPLETED
            tournament.save(update_fields=["status"])
            return

        champion = standings[0].user
        tournament.champion = champion
        tournament.status = Tournament.Status.COMPLETED

        # Announcement text
        week = tournament.week_number or "?"
        announcement_lines = [
            f"🏆 Gladiator Gauntlet Week {week} — Champion: "
            f"{champion.ai_name or champion.username} by @{champion.username}!",
        ]
        if len(standings) >= 2:
            r2 = standings[1].user
            announcement_lines.append(
                f"🥈 2nd: {r2.ai_name or r2.username} by @{r2.username}"
            )
        if len(standings) >= 3:
            r3 = standings[2].user
            announcement_lines.append(
                f"🥉 3rd: {r3.ai_name or r3.username} by @{r3.username}"
            )

        tournament.announcement = "\n".join(announcement_lines)
        tournament.save(update_fields=["champion", "status", "announcement"])

        # ── Award badges ─────────────────────────
        Badge.objects.create(
            user=champion,
            badge_type=Badge.BadgeType.GAUNTLET_CHAMPION,
            label=f"Gauntlet Champion Week {week}",
            tournament=tournament,
        )
        for s in standings[:3]:
            if s.user == champion:
                continue  # champion already has a badge
            Badge.objects.create(
                user=s.user,
                badge_type=Badge.BadgeType.GAUNTLET_TOP3,
                label=f"Gauntlet Top 3 Week {week}",
                tournament=tournament,
            )

        self.stdout.write(self.style.SUCCESS(
            f"\n  ✅ Gauntlet Week {week} complete!"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  🏆 Champion: {champion.username} "
            f"({standings[0].score} pts)"
        ))
        for s in standings[:3]:
            self.stdout.write(
                f"    #{s.rank} {s.user.username} — "
                f"{s.score} pts (W{s.wins} D{s.draws} L{s.losses})"
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  WebSocket broadcast helper
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _broadcast_standings(self, tournament: Tournament, round_num: int) -> None:
        """Push updated standings to the tournament WS group."""
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            channel_layer = get_channel_layer()
            if not channel_layer:
                return

            standings = list(
                tournament.standings
                .select_related("user")
                .order_by("rank")
            )
            data = {
                "type": "gauntlet_standings",
                "tournament_id": tournament.pk,
                "current_round": round_num,
                "rounds_total": tournament.rounds_total,
                "standings": [
                    {
                        "rank": s.rank,
                        "username": s.user.username,
                        "ai_name": s.user.ai_name or s.user.username,
                        "elo": s.user.elo,
                        "flag": s.user.country_flag,
                        "score": s.score,
                        "wins": s.wins,
                        "draws": s.draws,
                        "losses": s.losses,
                        "buchholz": s.buchholz,
                    }
                    for s in standings
                ],
            }
            async_to_sync(channel_layer.group_send)(
                f"tournament_{tournament.pk}",
                {"type": "tournament_event", "data": data},
            )
        except Exception as exc:
            log.warning("Could not broadcast standings: %s", exc)
