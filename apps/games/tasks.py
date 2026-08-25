# ──────────────────────────────────────────────
# apps/games/tasks.py
#
# Celery periodic tasks for the games app.
# Wire new tasks into CELERY_BEAT_SCHEDULE in settings.py.
# ──────────────────────────────────────────────
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Attempt Celery import; if unavailable, tasks are plain functions
# callable from management commands or cron scripts.
try:
    from celery import shared_task
except ImportError:
    def shared_task(func=None, **kwargs):
        """No-op decorator when Celery is not installed."""
        if func is not None:
            return func
        return lambda f: f


# Fire-and-forget — never block the beat worker on a slow/unreachable Space.
_KEEP_WARM_TIMEOUT = 5


def _ping_endpoint(base_url: str) -> None:
    """Best-effort HEAD request to *base_url* purely to keep the model warm.

    Any failure (timeout, connection error, non-2xx) is swallowed — this
    is a warmth signal, not a correctness check.
    """
    import requests

    try:
        requests.head(base_url, timeout=_KEEP_WARM_TIMEOUT)
    except requests.exceptions.RequestException:
        pass


@shared_task
def keep_warm_ongoing_games() -> str:
    """Ping each side's HF Space endpoint for every ongoing Game.

    Fires roughly every 5 minutes via Celery Beat so the serverless HF
    Spaces backing live bot games don't go cold between moves.
    """
    from apps.games.bot_runner import _get_repo_for_user
    from apps.games.models import Game
    from apps.games.predict_breakthrough import _space_base_url as _bt_space_url
    from apps.games.predict_chess import _space_url_for as _chess_space_url

    pinged = 0
    games = Game.objects.filter(status=Game.Status.ONGOING).select_related("white", "black")
    for game in games:
        for user in (game.white, game.black):
            if user is None:
                continue
            repo = _get_repo_for_user(user, game.game_type)
            if not repo:
                continue
            try:
                base_url = (
                    _bt_space_url(repo) if game.game_type == "breakthrough"
                    else _chess_space_url(repo)
                )
            except Exception:
                log.debug("keep_warm_ongoing_games: could not resolve URL for repo=%s", repo)
                continue
            _ping_endpoint(base_url)
            pinged += 1

    log.debug("keep_warm_ongoing_games: pinged %d endpoint(s)", pinged)
    return f"Pinged {pinged} endpoint(s)."
