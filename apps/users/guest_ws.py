"""WebSocket gate for guest accounts: only the lobby, game, spectate, presence and read-only feeds."""
from __future__ import annotations

import re

from django.conf import settings

GUEST_WS_ALLOWED = re.compile(
    r"^/?ws/(notifications/|leaderboard/|presence/|games/lobby/|games/\d+/|games/\d+/spectate/)$"
)


class GuestWebSocketGate:
    """Wrap the URLRouter (inside AuthMiddlewareStack) so ``scope['user']`` is already resolved."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            user = scope.get("user")
            if getattr(user, "is_authenticated", False) and getattr(user, "is_guest", False):
                allowed = getattr(settings, "GUEST_MODE_ENABLED", False) and GUEST_WS_ALLOWED.match(scope.get("path", ""))
                if not allowed:
                    await receive()  # websocket.connect
                    await send({"type": "websocket.close", "code": 4403})
                    return
        return await self.inner(scope, receive, send)
