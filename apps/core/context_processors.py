from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from django.urls import reverse


def platform_limits(request):
    """Expose the sandbox and qualification limits so the documentation pages never hard-code them."""
    # The two worker settings are not in settings.py; the defaults match apps.games.sandbox_runner.
    return {
        "sandbox_move_timeout": int(getattr(settings, "SANDBOX_MOVE_TIMEOUT", 30)),
        "sandbox_memory_mb": int(getattr(settings, "SANDBOX_MEMORY_LIMIT_MB", 512)),
        "sandbox_cpu": float(getattr(settings, "SANDBOX_CPU_LIMIT", 1.0)),
        "sandbox_pids": int(getattr(settings, "SANDBOX_PIDS_LIMIT", 128)),
        "sandbox_cold_start_timeout": int(float(getattr(settings, "SANDBOX_WORKER_START_TIMEOUT", 120))),
        "sandbox_idle_minutes": float(getattr(settings, "SANDBOX_WORKER_IDLE_SECONDS", 900)) / 60,
        "guest_snapshot_idle_hours": float(getattr(settings, "GUEST_SNAPSHOT_IDLE_MINUTES", 120)) / 60,
        "guest_ttl_days": int(getattr(settings, "GUEST_TTL_DAYS", 3)),
        "tournament_min_rated_games": int(getattr(settings, "TOURNAMENT_MIN_RATED_GAMES", 30)),
    }


def active_play(request):
    """Inject active_play and prize_claim_alert into every template for logged-in users."""
    if not request.user.is_authenticated:
        return {"active_play": None, "prize_claim_alert": None}
    return {
        "active_play": _get_user_active_play(request.user),
        "prize_claim_alert": _get_prize_claim_alert(request.user),
    }


def _get_user_active_play(user) -> dict | None:
    """Return the user's most relevant active game or match, or None.

    Priority:
      1. Live tournament Match (player1 or player2, non-armageddon)
      2. Ongoing casual Game (white or black)
    """
    # Query 1: live tournament match (single JOIN on indexed FK columns)
    from apps.tournaments.models import Match
    match = (
        Match.objects
        .filter(
            Q(player1=user) | Q(player2=user),
            match_status=Match.MatchStatus.LIVE,
            is_armageddon=False,
        )
        .select_related("tournament")
        .first()
    )
    if match is not None:
        return {
            "type": "tournament_match",
            "url": reverse("tournaments:live_match", args=[match.tournament.pk, match.pk]),
            "tournament_name": match.tournament.name,
            "round_num": match.round_num,
        }

    # Query 2: ongoing casual game (single JOIN on indexed FK columns)
    from apps.games.models import Game
    game = (
        Game.objects
        .filter(
            Q(white=user) | Q(black=user),
            status=Game.Status.ONGOING,
        )
        .select_related("white", "black")
        .first()
    )
    if game is not None:
        opponent = game.black if game.white_id == user.pk else game.white
        return {
            "type": "casual_game",
            "url": reverse("games:game_detail", args=[game.pk]),
            "opponent": opponent.username if opponent else "?",
            "game_type": game.game_type,
        }

    return None


def _get_prize_claim_alert(user) -> dict | None:
    """Return a PENDING prize claim the user must action, or None."""
    from apps.tournaments.models import PrizeClaim, Tournament

    # Bug 1 fix: only the confirmed champion of a completed tournament qualifies.
    tournament = (
        Tournament.objects
        .filter(champion=user, status=Tournament.Status.COMPLETED)
        .order_by("-id")
        .first()
    )
    if tournament is None:
        return None

    claim = PrizeClaim.objects.filter(tournament=tournament).first()
    # Bug 2 fix: only show the badge while the claim is still PENDING.
    if claim is None or claim.status != PrizeClaim.Status.PENDING:
        return None
    if tournament.prize_on_hold:
        return None

    return {
        "url": reverse("tournaments:prize_claim", args=[tournament.pk]),
        "tournament_name": tournament.name,
    }
