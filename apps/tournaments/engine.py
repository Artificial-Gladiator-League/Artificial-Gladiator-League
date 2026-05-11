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
from typing import TYPE_CHECKING

import chess
from django.db import models, transaction
from django.utils import timezone

from apps.games.breakthrough_engine import STARTING_FEN as BT_STARTING_FEN

if TYPE_CHECKING:
    from apps.users.models import CustomUser

from apps.games.models import Game
from .models import Match, Tournament, TournamentParticipant

log = logging.getLogger(__name__)


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
    Players who were disqualified between rounds (eliminated=True) are
    excluded from subsequent pairings; a bye is awarded to their would-be
    opponent if needed.
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
        # Collect eliminated user ids so disqualified players are skipped.
        eliminated_ids = set(
            tournament.participants
            .filter(eliminated=True)
            .values_list("user_id", flat=True)
        )
        # Get winners from the previous round (skip disqualified ones)
        prev_matches = (
            tournament.matches
            .filter(round_num=round_num - 1, is_armageddon=False)
            .exclude(winner__isnull=True)
            .order_by("bracket_position")
            .select_related("winner")
        )
        players = [
            m.winner
            for m in prev_matches
            if m.winner and m.winner_id not in eliminated_ids
        ]

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
            ai_thinking_seconds=1.0,
        )

        # Launch the AI bot game in a background thread
        _start_bot_game_thread(game.pk)

        matches.append(match)

    tournament.current_round = round_num
    tournament.save(update_fields=["current_round"])

    # ── Anti-cheat: pin baseline SHA for every active participant.
    # Done after current_round is persisted so the audit log records
    # the correct round_num. Failures here must never abort the round.
    try:
        from apps.tournaments.sha_audit import (
            capture_round_baseline, schedule_round_integrity_check,
        )
        capture_round_baseline(tournament, round_num)
    except Exception:
        log.exception(
            "capture_round_baseline failed for tournament=%s round=%d",
            tournament.pk, round_num,
        )
    else:
        # Guarantee at least one randomised integrity check per round,
        # fired at a random offset inside the round window. Skipped for
        # QA tournaments (which have no anti-cheat gate).
        if tournament.type != Tournament.Type.QA:
            try:
                schedule_round_integrity_check(tournament, round_num)
            except Exception:
                log.exception(
                    "schedule_round_integrity_check failed for "
                    "tournament=%s round=%d",
                    tournament.pk, round_num,
                )

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
            # Static checks
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

    # ── Pre-round-1 SHA check (registered SHA vs current live SHA) ──
    pre_round_sha_check(tournament)

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
            # ── Pre-round SHA check before generating next round ──
            pre_round_sha_check(tournament)
            # Re-count after potential SHA disqualifications
            active_players = tournament.participants.filter(eliminated=False).count()
            if active_players <= 1:
                _complete_tournament(tournament)
            else:
                # Generate next round
                generate_pairings(tournament, round_num + 1)


@transaction.atomic
def _complete_tournament(tournament: Tournament) -> None:
    """Mark the tournament as completed, set the champion, and distribute prizes.

    Also cleans up all integrity-related fields (tournament_hf_token and
    registered_sha) on every TournamentParticipant so that sensitive data
    is not retained after the tournament ends.
    """
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

    # ── Integrity field cleanup (tokens + SHA records deleted at end) ──
    try:
        tournament.participants.update(
            tournament_hf_token="",
            registered_sha="",
        )
        log.info(
            "Cleared tournament_hf_token + registered_sha for all participants "
            "of tournament %s (pk=%s)",
            tournament.name, tournament.pk,
        )
    except Exception:
        log.exception(
            "Failed to clear integrity fields for tournament %s", tournament.pk
        )

    log.info(
        "Tournament %s completed — Champion: %s",
        tournament.name,
        champion.username if champion else "N/A",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-round and mid-round SHA integrity checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _notify_disqualification(user, reason: str) -> None:
    """Push a WebSocket notification to *user* explaining the disqualification.

    Best-effort — failures are logged but never raised.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from apps.core.consumers import notif_group_name

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            notif_group_name(user.pk),
            {
                "type": "send_notification",
                "verb": "tournament_disqualification",
                "actor": "",
                "message": (
                    "⚠️ You have been disqualified from a tournament. "
                    f"Reason: {reason} "
                    "Your model repository must remain unchanged throughout the tournament. "
                    "See the Fair Play Policy for details."
                ),
                "url": "",
                # Force the user's browser back to the lobby on the next
                # notification frame — see base.html new_notification handler.
                "redirect_url": "/games/lobby/",
                "unread_count": 0,
            },
        )
    except Exception:
        log.debug("Could not send disqualification notification to user %s", user.pk, exc_info=True)


@transaction.atomic
def pre_round_sha_check(tournament: Tournament) -> list[tuple]:
    """Fire-and-forget SHA integrity check for every active participant.

    Called from ``start_tournament`` (before round 1) and from
    ``handle_game_result`` (between rounds). The check itself runs in
    Celery workers via ``run_sha_check_for_participant.delay`` so this
    function never blocks game generation.

    Each worker calls ``apps.tournaments.sha_audit.perform_sha_check``
    which:
      * compares the participant's pinned baseline (or
        ``gm.approved_full_sha``) against the live HF commit,
      * writes a ``TournamentShaCheck`` audit row,
      * on mismatch invokes ``_handle_mid_round_disqualification`` to
        eliminate the participant + forfeit any live match, and resets
        ``rated_games_since_revalidation = 0`` so the user must replay
        ``REVALIDATION_GAMES_REQUIRED`` (30) rated games before they
        can rejoin any tournament (enforced by
        ``apps.users.integrity.can_join_tournament``).

    Returns the list of ``(username, dispatch_outcome)`` tuples for
    structured logging — the actual PASS/FAIL outcome lives in the
    audit table once the worker completes.
    """
    if tournament.type == Tournament.Type.QA:
        return []

    dispatched: list[tuple] = []

    candidates = list(
        tournament.participants
        .filter(eliminated=False, disqualified_for_sha_mismatch=False)
        .select_related("user")
    )
    if not candidates:
        return dispatched

    # Resolve the Celery task lazily so the function still works in
    # environments where Celery is not configured (it falls back to
    # an inline call so SHA enforcement is never silently skipped).
    delay_fn = None
    try:
        from apps.tournaments.tasks import run_sha_check_for_participant
        delay_fn = getattr(run_sha_check_for_participant, "delay", None)
    except Exception:
        delay_fn = None

    for p in candidates:
        if delay_fn is not None:
            try:
                delay_fn(p.pk)
                dispatched.append((p.user.username, "dispatched"))
                continue
            except Exception:
                log.exception(
                    "pre_round_sha_check: failed to enqueue async check "
                    "for participant=%s — running inline",
                    p.pk,
                )
        # Inline fallback: runs in-process. Synchronous, but only when
        # Celery is unavailable. Eliminations still happen.
        try:
            from apps.tournaments.sha_audit import perform_sha_check
            row = perform_sha_check(p, context="random_audit")
            dispatched.append(
                (p.user.username, row.result if row else "skipped"),
            )
        except Exception:
            log.exception(
                "pre_round_sha_check: inline check failed for participant=%s",
                p.pk,
            )
            dispatched.append((p.user.username, "error"))

    log.info(
        "pre_round_sha_check: tournament=%s round=%d dispatched=%d",
        tournament.pk, tournament.current_round, len(dispatched),
    )
    return dispatched


@transaction.atomic
def _handle_mid_round_disqualification(
    tournament: Tournament,
    participant,
    reason: str,
) -> None:
    """Disqualify a participant during an active round.

    Steps (all in one transaction):
      1. Find their current active game (if any) and forfeit it — opponent
         receives a walkover.  The post-save signal handles ELO updates and
         bracket advancement.
      2. Mark the participant eliminated.
      3. Notify the disqualified user via WebSocket.
      4. Other active games and participants are completely unaffected.
    """
    user = participant.user
    log.warning(
        "Mid-round disqualification: user=%s tournament=%s reason=%s",
        user.username, tournament.pk, reason,
    )

    # ── Step 1: forfeit their active game ─────────────────────
    # Find the live Match for this player in the current round
    current_round = tournament.current_round
    live_match = (
        tournament.matches
        .filter(
            round_num=current_round,
            match_status=Match.MatchStatus.LIVE,
            is_armageddon=False,
        )
        .filter(
            models.Q(player1=user) | models.Q(player2=user),
        )
        .select_related("player1", "player2")
        .first()
    )

    if live_match:
        # Determine the opponent
        opponent = live_match.player2 if live_match.player1 == user else live_match.player1

        # Find the active Game linked to this match
        from apps.games.models import Game

        active_game = (
            live_match.games
            .exclude(status__in=Game.TERMINAL_STATUSES)
            .select_related("white", "black")
            .first()
        )

        if active_game:
            # Award the game to the opponent
            if active_game.white == user:
                active_game.status = Game.Status.BLACK_WINS
                active_game.result = Game.Result.BLACK_WIN
                active_game.winner = active_game.black
            else:
                active_game.status = Game.Status.WHITE_WINS
                active_game.result = Game.Result.WHITE_WIN
                active_game.winner = active_game.white

            active_game.result_reason = "disqualification"
            # Save triggers post_save → update_stats_after_game → handle_game_result
            active_game.save()

            log.info(
                "Forfeited game %s — walkover awarded to %s (disqualification of %s)",
                active_game.pk, opponent.username, user.username,
            )
        else:
            # No live game — close the match directly with the opponent as winner
            live_match.winner = opponent
            live_match.result = "walkover"
            live_match.match_status = Match.MatchStatus.COMPLETED
            live_match.save(update_fields=["winner", "result", "match_status"])
    else:
        # No live match — simply mark eliminated (between rounds or already done)
        pass

    # ── Step 2: mark participant eliminated ───────────────────
    participant.eliminated = True
    participant.eliminated_in_round = current_round
    participant.save(update_fields=["eliminated", "eliminated_in_round"])

    # ── Step 3: notify the user ───────────────────────────────
    _notify_disqualification(user, reason)

    log.info(
        "Mid-round disqualification complete for user=%s tournament=%s",
        user.username, tournament.pk,
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
