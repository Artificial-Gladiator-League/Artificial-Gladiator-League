# ──────────────────────────────────────────────
# apps/tournaments/engine.py
#
# Tournament bracket logic: pairing generation,
# winner advancement, round completion, and
# tournament lifecycle management.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging
import random
import threading
from decimal import Decimal
from typing import TYPE_CHECKING

import chess
from django.db import transaction
from django.utils import timezone

from apps.games.breakthrough_engine import STARTING_FEN as BT_STARTING_FEN

if TYPE_CHECKING:
    from apps.users.models import CustomUser

from apps.games.models import Game
from .models import Match, Tournament, TournamentParticipant

log = logging.getLogger(__name__)

# ── Prize structure ──────────────────────────
SMALL_PRIZE_1ST = Decimal("15.00")
LARGE_PRIZES = {
    1: Decimal("30.00"),
    2: Decimal("20.00"),
    3: Decimal("10.00"),
}


def _start_bot_game_thread(game_id: int) -> None:
    """Launch a background thread to run the AI bot game loop.

    Uses transaction.on_commit so the thread only starts after the
    current DB transaction has committed (ensuring the Game row exists).
    """
    from apps.games.bot_runner import run_bot_game

    def _launch():
        t = threading.Thread(target=run_bot_game, args=(game_id,), daemon=True)
        t.start()

    transaction.on_commit(_launch)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pairing generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def generate_pairings(tournament: Tournament, round_num: int) -> list[Match]:
    """Create Match objects for a given round of a single-elimination bracket.

    For round 1, seeds are taken from TournamentParticipant.
    For later rounds, winners of the previous round are paired.
    Colours are assigned randomly for each match.

    Returns the list of newly created Match instances.
    """
    if round_num == 1:
        participants = list(
            tournament.participants
            .filter(eliminated=False)
            .select_related("user")
            .order_by("seed")
        )
        players = [p.user for p in participants]
    else:
        # Get winners from the previous round
        prev_matches = (
            tournament.matches
            .filter(round_num=round_num - 1, is_armageddon=False)
            .exclude(winner__isnull=True)
            .order_by("bracket_position")
            .select_related("winner")
        )
        players = [m.winner for m in prev_matches]

    # Pad to even count with byes (should not happen with power-of-2 brackets)
    if len(players) % 2 != 0:
        players.append(None)

    matches = []
    for pos in range(0, len(players), 2):
        p1 = players[pos]
        p2 = players[pos + 1]

        # Handle byes: if one side is None, the other auto-advances
        if p1 is None and p2 is None:
            continue
        if p1 is None or p2 is None:
            winner = p1 or p2
            match = Match.objects.create(
                tournament=tournament,
                round_num=round_num,
                bracket_position=pos // 2,
                player1=winner,
                player2=winner,  # placeholder — bye
                winner=winner,
                result="bye",
                match_status=Match.MatchStatus.COMPLETED,
            )
            matches.append(match)
            continue

        # Random colour assignment
        if random.random() < 0.5:
            white, black = p1, p2
        else:
            white, black = p2, p1

        tc = tournament.time_control or "3+1"
        parts = tc.split("+")
        base_sec = int(parts[0]) * 60 if parts else 180
        inc = int(parts[1]) if len(parts) > 1 else 0

        match = Match.objects.create(
            tournament=tournament,
            round_num=round_num,
            bracket_position=pos // 2,
            player1=white,
            player2=black,
            match_status=Match.MatchStatus.LIVE,
            time_control=tc,
        )

        # Create the linked Game with full board state
        gt = tournament.game_type
        starting_fen = BT_STARTING_FEN if gt == "breakthrough" else chess.STARTING_FEN
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
            game_type=gt,
            current_fen=starting_fen,
        )

        # Launch the AI bot game in a background thread
        _start_bot_game_thread(game.pk)

        matches.append(match)

    tournament.current_round = round_num
    tournament.save(update_fields=["current_round"])

    return matches


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Winner advancement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@transaction.atomic
def advance_winners(tournament: Tournament, round_num: int) -> None:
    """Mark losers as eliminated and update participant records."""
    matches = tournament.matches.filter(
        round_num=round_num, is_armageddon=False,
    ).select_related("player1", "player2", "winner")

    for match in matches:
        if not match.winner:
            continue
        loser = match.player2 if match.winner == match.player1 else match.player1
        if loser == match.winner:
            continue  # bye

        TournamentParticipant.objects.filter(
            tournament=tournament, user=loser,
        ).update(
            eliminated=True,
            eliminated_in_round=round_num,
            current_round=round_num,
        )

        TournamentParticipant.objects.filter(
            tournament=tournament, user=match.winner,
        ).update(current_round=round_num)


def is_round_complete(tournament: Tournament, round_num: int) -> bool:
    """Check whether all non-Armageddon matches in a round have a winner."""
    matches = tournament.matches.filter(
        round_num=round_num, is_armageddon=False,
    )
    return matches.exists() and not matches.filter(winner__isnull=True).exists()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tournament lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REVALIDATION_GAMES_REQUIRED = 30


def _disqualify_ineligible_participants(tournament: Tournament) -> list:
    """Remove participants whose model fails integrity checks before start.

    For every participant:
      1. Look up their UserGameModel for tournament.game_type.
      2. Fetch their stored HF token.
      3. If the token exists, run validate_model_integrity — if it fails,
         mark the participant eliminated.
      4. Also disqualify if model_integrity_ok is False or
         rated_games_since_revalidation < 30.

    QA tournaments skip all checks.
    Returns a list of (username, reason) tuples for logging.
    """
    if tournament.type == Tournament.Type.QA:
        return []

    from apps.users.integrity import _get_stored_token, validate_model_integrity
    from apps.users.models import UserGameModel
    from django.core.mail import mail_admins

    game_type = tournament.game_type
    disqualified = []

    for participant in tournament.participants.select_related("user").all():
        user = participant.user
        reason = None

        try:
            game_model = UserGameModel.objects.get(
                user=user, game_type=game_type,
            )
        except UserGameModel.DoesNotExist:
            reason = "No model submitted for this game type."
        else:
            # Live HF integrity check if we have a token
            token = _get_stored_token(user)
            if token:
                ok, msg = validate_model_integrity(game_model, token)
                if not ok:
                    reason = msg
            # Static checks (even if no token or live check passed)
            if reason is None and not game_model.model_integrity_ok:
                reason = "Model integrity flag is False."
            if reason is None and game_model.rated_games_since_revalidation < REVALIDATION_GAMES_REQUIRED:
                reason = (
                    f"Only {game_model.rated_games_since_revalidation} / "
                    f"{REVALIDATION_GAMES_REQUIRED} revalidation games played."
                )

        if reason:
            disqualified.append((user.username, reason))
            participant.eliminated = True
            participant.eliminated_in_round = 0
            participant.save(update_fields=["eliminated", "eliminated_in_round"])
            tournament.players.remove(user)
            log.warning(
                "🚫 Disqualified %s from tournament %s: %s",
                user.username, tournament.name, reason,
            )

    # Notify admins about disqualifications
    if disqualified:
        body = (
            f"Tournament: {tournament.name} (pk={tournament.pk})\n"
            f"Game type: {game_type}\n\n"
            "Disqualified players:\n"
        )
        for username, reason in disqualified:
            body += f"  • {username}: {reason}\n"

        try:
            mail_admins(
                subject=f"[AGL] {len(disqualified)} player(s) disqualified — {tournament.name}",
                message=body,
            )
        except Exception:
            log.debug("Could not send admin notification", exc_info=True)

    return disqualified


@transaction.atomic
def start_tournament(tournament: Tournament) -> list[Match]:
    """Kick off a tournament: shuffle seeds and generate round‑1 pairings.

    Before starting, all participants are checked for model integrity
    in the tournament's game_type.  Players who fail are removed.

    Precondition: tournament.is_full or start_time has passed with ≥ 2 players.
    """
    # ── Pre-start integrity gate: check ALL participants ──
    disqualified = _disqualify_ineligible_participants(tournament)
    if disqualified:
        log.info(
            "Tournament %s: %d player(s) disqualified before start: %s",
            tournament.name, len(disqualified),
            ", ".join(f"{u} ({r})" for u, r in disqualified),
        )

    # Abort if fewer than 2 eligible players remain
    if tournament.participant_count < 2:
        tournament.status = Tournament.Status.COMPLETED
        tournament.save(update_fields=["status"])
        log.warning(
            "Tournament %s aborted — only %d eligible player(s) after integrity check.",
            tournament.name, tournament.participant_count,
        )
        return []

    participants = list(tournament.participants.filter(eliminated=False))
    random.shuffle(participants)

    # Assign random seeds
    for i, p in enumerate(participants):
        p.seed = i
    TournamentParticipant.objects.bulk_update(participants, ["seed"])

    tournament.status = Tournament.Status.ONGOING
    tournament.save(update_fields=["status"])

    return generate_pairings(tournament, round_num=1)


@transaction.atomic
def handle_game_result(game: Game) -> None:
    """Called when a tournament Game finishes.

    Updates the linked Match, advances the bracket if the round is
    complete, and handles Armageddon creation for draws.
    """
    match = game.tournament_match
    if match is None:
        return

    tournament = match.tournament

    # If the game drew, create an Armageddon game
    if game.result == Game.Result.DRAW and game.armageddon_of is None:
        from apps.games.chess_engine import create_armageddon
        arm = create_armageddon(game)
        # Propagate game_type and correct starting FEN
        arm.game_type = game.game_type
        if game.game_type == "breakthrough":
            arm.current_fen = BT_STARTING_FEN
        arm.save()

        # Create a corresponding Armageddon Match record
        Match.objects.create(
            tournament=tournament,
            round_num=match.round_num,
            bracket_position=match.bracket_position,
            player1=arm.white,
            player2=arm.black,
            match_status=Match.MatchStatus.LIVE,
            is_armageddon=True,
            time_control="2+0",
        )

        # Launch AI bots for the Armageddon game
        _start_bot_game_thread(arm.pk)

        return  # wait for Armageddon to finish

    # Decisive result — update the match
    match.result = game.result
    match.winner = game.winner
    match.match_status = Match.MatchStatus.COMPLETED
    # Read ELO deltas from the DB row — the Game signal uses .update()
    # which does not modify the in-memory instance.
    db_deltas = Game.objects.filter(pk=game.pk).values_list(
        "elo_change_white", "elo_change_black",
    ).first()
    match.elo_change_p1 = db_deltas[0] if db_deltas else 0
    match.elo_change_p2 = db_deltas[1] if db_deltas else 0
    match.save(update_fields=[
        "result", "winner", "match_status",
        "elo_change_p1", "elo_change_p2",
    ])

    # If this was an Armageddon, also close the parent match
    if game.armageddon_of is not None:
        parent_game = game.armageddon_of
        parent_match = parent_game.tournament_match
        if parent_match and not parent_match.winner:
            parent_match.winner = game.winner
            parent_match.match_status = Match.MatchStatus.COMPLETED
            parent_match.save(update_fields=["winner", "match_status"])

    # Check if the round is complete
    round_num = match.round_num
    if is_round_complete(tournament, round_num):
        advance_winners(tournament, round_num)

        # Is this the final round?
        active_players = tournament.participants.filter(eliminated=False).count()
        if active_players <= 1:
            _complete_tournament(tournament)
        else:
            # Generate next round
            generate_pairings(tournament, round_num + 1)


@transaction.atomic
def _complete_tournament(tournament: Tournament) -> None:
    """Mark the tournament as completed, set the champion, and distribute prizes."""
    champion_entry = (
        tournament.participants
        .filter(eliminated=False)
        .select_related("user")
        .first()
    )
    champion = champion_entry.user if champion_entry else None

    tournament.champion = champion
    tournament.status = Tournament.Status.COMPLETED
    tournament.save(update_fields=["champion", "status"])

    # Prize distribution
    _distribute_prizes(tournament)

    log.info(
        "Tournament %s completed — Champion: %s",
        tournament.name,
        champion.username if champion else "N/A",
    )


def _distribute_prizes(tournament: Tournament) -> None:
    """Log prize awards. Small: $15 to 1st. Large: $30/$20/$10 to 1st/2nd/3rd.

    In production this would trigger a payout via payment provider.
    """
    if tournament.type == Tournament.Type.SMALL:
        if tournament.champion:
            log.info(
                "Prize: %s awarded $%s (1st) in %s",
                tournament.champion.username,
                SMALL_PRIZE_1ST,
                tournament.name,
            )
    elif tournament.type == Tournament.Type.LARGE:
        # 1st = champion, 2nd = finalist, 3rd = both semi-final losers
        final_round = tournament.rounds_total
        final_match = (
            tournament.matches
            .filter(round_num=final_round, is_armageddon=False)
            .select_related("player1", "player2", "winner")
            .first()
        )
        if final_match and final_match.winner:
            runner_up = (
                final_match.player2
                if final_match.winner == final_match.player1
                else final_match.player1
            )
            log.info(
                "Prize: %s awarded $%s (1st) in %s",
                final_match.winner.username,
                LARGE_PRIZES[1],
                tournament.name,
            )
            log.info(
                "Prize: %s awarded $%s (2nd) in %s",
                runner_up.username,
                LARGE_PRIZES[2],
                tournament.name,
            )

        # Semi-final losers (3rd place)
        semi_round = final_round - 1
        semi_matches = (
            tournament.matches
            .filter(round_num=semi_round, is_armageddon=False)
            .select_related("player1", "player2", "winner")
        )
        for sm in semi_matches:
            if sm.winner:
                loser = (
                    sm.player2 if sm.winner == sm.player1 else sm.player1
                )
                if loser != sm.winner:
                    log.info(
                        "Prize: %s awarded $%s (3rd) in %s",
                        loser.username,
                        LARGE_PRIZES[3],
                        tournament.name,
                    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Simulation helper (for management command)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def simulate_game(p1: CustomUser, p2: CustomUser) -> tuple[str, CustomUser | None]:
    """Play a full game with random legal moves using python-chess.

    Returns (result_string, winner_or_None).
    """
    board = chess.Board()
    for _ in range(200):
        if board.is_game_over():
            break
        move = random.choice(list(board.legal_moves))
        board.push(move)

    outcome = board.outcome()
    if outcome is None or outcome.winner is None:
        return "1/2-1/2", None
    if outcome.winner == chess.WHITE:
        return "1-0", p1
    return "0-1", p2


def simulate_armageddon(p1: CustomUser, p2: CustomUser) -> tuple[str, CustomUser]:
    """Simulate an Armageddon tiebreak (random colours, draw = Black wins)."""
    players = [p1, p2]
    random.shuffle(players)
    arm_white, arm_black = players

    result, winner = simulate_game(arm_white, arm_black)
    if result == "1/2-1/2":
        # Draw odds: Black wins
        return "0-1", arm_black
    return result, winner
