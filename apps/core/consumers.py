import json

from channels.generic.websocket import AsyncWebsocketConsumer


class LeaderboardConsumer(AsyncWebsocketConsumer):
    """
    Real‑time leaderboard — clients join the 'leaderboard' group
    and receive push updates whenever a rated game finishes.
    """

    GROUP = "leaderboard"

    async def connect(self):
        await self.channel_layer.group_add(self.GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # Clients don't send anything meaningful; ignore.
        pass

    # ── Group message handler ──────────────────
    async def leaderboard_update(self, event):
        """Forward the refresh signal to the browser."""
        await self.send(text_data=json.dumps(event["data"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PresenceConsumer — online player tracking
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_online_connections: dict[str, int] = {}   # channel_name → user_id
_online_users: set[int] = set()            # unique user IDs currently connected


class PresenceConsumer(AsyncWebsocketConsumer):
    """
    Tracks how many authenticated users are connected and broadcasts
    the count in real-time to all connected clients.
    """

    GROUP = "presence"

    async def connect(self):
        self.user = self.scope.get("user")
        await self.channel_layer.group_add(self.GROUP, self.channel_name)
        await self.accept()

        if self.user and not self.user.is_anonymous:
            _online_connections[self.channel_name] = self.user.id
            _online_users.add(self.user.id)

        await self._send_count()
        await self._broadcast_count()

    async def disconnect(self, close_code):
        if self.channel_name in _online_connections:
            uid = _online_connections.pop(self.channel_name)
            # Only remove user from online set if no other connections remain
            if uid not in _online_connections.values():
                _online_users.discard(uid)

        await self._broadcast_count()
        await self.channel_layer.group_discard(self.GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        pass

    async def _send_count(self):
        """Send the current count to this client only."""
        await self.send(text_data=json.dumps({
            "type": "online_count",
            "count": len(_online_users),
        }))

    async def _broadcast_count(self):
        """Broadcast the updated count to every connected client."""
        await self.channel_layer.group_send(self.GROUP, {
            "type": "presence_update",
            "data": {
                "type": "online_count",
                "count": len(_online_users),
            },
        })

    async def presence_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
