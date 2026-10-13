# ──────────────────────────────────────────────
# apps/games/platform_fault.py
#
# A game is cancelled ONLY when the Docker sandbox itself is unavailable
# (SandboxUnavailableError) - that is a platform problem, never the player's
# fault. Nothing a player's own program does (timeout, crash, no move, illegal
# move) may reach this module; those keep the existing random-move / forfeit
# behaviour in predict_chess / predict_breakthrough / bot_runner.
#
# A cancelled game is ABORTED with no result, so the rating signal skips it
# (apps.users.signals.update_stats_after_game returns early for result "*"):
# no Elo change and no rated-game counter increment.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging

from django.db import transaction

log = logging.getLogger(__name__)

PLATFORM_ERROR = "platform_error"
REASON = "sandbox_unavailable"

CASUAL_MESSAGE = (
    "This game was cancelled because the platform's sandbox is unavailable. "
    "Your rating was not changed."
)
POSTPONED_MESSAGE = (
    "Match postponed because of a platform problem. The organizers have been notified."
)


def replay_message(attempt: int, max_attempts: int, delay: int) -> str:
    return (
        "Game cancelled because of a platform problem. "
        f"A replacement game starts in {delay} seconds (attempt {attempt} of {max_attempts})."
    )


def cancel_game(game_id: int):
    """Abort an unfinished game with no result. Returns the Game, or None if it was already finished."""
    from apps.games.models import Game

    with transaction.atomic():
        game = Game.objects.select_for_update().select_related("white", "black").get(pk=game_id)
        if game.is_finished:
            return None
        game.status = Game.Status.ABORTED
        game.result = Game.Result.NONE
        game.winner = None
        game.result_reason = PLATFORM_ERROR
        game.save(update_fields=["status", "result", "winner", "result_reason"])
    return game


def handle_sandbox_unavailable(game_id: int) -> dict | None:
    """Cancel *game_id* because the sandbox is unavailable; return the message payload for viewers.

    Returns None when the game was already finished (nothing to do). A tournament game is
    replayed or its match postponed by the tournament engine.
    """
    game = cancel_game(game_id)
    if game is None:
        return None

    payload = {
        "type": "game_aborted",
        "game_id": game.pk,
        "match_id": game.tournament_match_id,
        "reason": REASON,
        "message": CASUAL_MESSAGE,
        "postponed": False,
        "replacement_game_id": None,
        "replacement_in": None,
    }
    log.error("[game %s] cancelled: the sandbox is unavailable (no rating change)", game.pk)

    if game.is_tournament_game and game.tournament_match_id:
        from apps.tournaments.engine import handle_cancelled_game

        outcome = handle_cancelled_game(game)
        if outcome["postponed"]:
            payload["message"] = POSTPONED_MESSAGE
            payload["postponed"] = True
        else:
            payload["message"] = replay_message(outcome["attempt"], outcome["max_attempts"], outcome["delay"])
            payload["replacement_game_id"] = outcome["replacement_game_id"]
            payload["replacement_in"] = outcome["delay"]
    return payload


def broadcast_game_aborted(payload: dict) -> None:
    """Tell everyone watching the game (and its tournament match page) that it was cancelled. Best-effort."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f"game_{payload['game_id']}", {"type": "broadcast_game_aborted", "data": payload},
        )
        if payload.get("match_id"):
            async_to_sync(layer.group_send)(
                f"match_{payload['match_id']}", {"type": "game_aborted", "data": payload},
            )
    except Exception:
        log.exception("Could not broadcast the cancelled game %s", payload.get("game_id"))
