import json

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer


# ── Notification helpers ───────────────────────────────────────────────────────

def notif_group_name(user_id: int) -> str:
    """Return the channel-layer group name for a user's notification stream."""
    return f"notifications_{user_id}"


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    Per-user WebSocket consumer that forwards server-push notifications to the
    browser (toasts, redirects, etc.).  Connected from base.html at
    /ws/notifications/.  The server sends group messages with
    type='send_notification'; this consumer re-emits them as type='new_notification'
    so the front-end JS handler can react.
    """

    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or self.user.is_anonymous:
            await self.close()
            return
        self.group = notif_group_name(self.user.pk)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # Clients do not send anything to this endpoint.
        pass

    async def send_notification(self, event):
        """Handle group_send messages and forward them to the browser..."""
        # DQ events are handled by LiveMatchConsumer; suppress here to avoid a racing toast+redirect.
        if event.get("verb") == "tournament_disqualified":
            return
        await self.send(text_data=json.dumps({
            "type": "new_notification",
            "verb": event.get("verb", ""),
            "actor": event.get("actor", ""),
            "message": event.get("message", ""),
            "url": event.get("url", ""),
            "redirect_url": event.get("redirect_url", ""),
            "unread_count": event.get("unread_count", 0),
        }))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LeaderboardConsumer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

    async def leaderboard_update(self, event):
        """Forward the refresh signal to the browser."""
        await self.send(text_data=json.dumps(event["data"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PresenceConsumer — online player tracking
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_online_connections: dict[str, int] = {}   # channel_name → user_id
_online_users: set[int] = set()            # unique user IDs currently connected


def presence_user_group(user_id: int) -> str:
    return f"presence_user_{user_id}"


async def _drop_local(user_id: int) -> None:
    """Remove the user from THIS process's presence state and broadcast the count."""
    for ch in [ch for ch, uid in _online_connections.items() if uid == user_id]:
        _online_connections.pop(ch, None)
    if user_id not in _online_connections.values():
        _online_users.discard(user_id)
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        await channel_layer.group_send("presence", {
            "type": "presence_update",
            "data": {"type": "online_count", "count": len(_online_users)},
        })


async def remove_user_presence(user_id: int) -> None:
    """Callable from ANY process (e.g. gunicorn on logout): ask the process that
    holds the user's sockets (daphne) to drop them."""
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        await channel_layer.group_send(
            presence_user_group(user_id), {"type": "presence_force_remove"}
        )


class PresenceConsumer(AsyncWebsocketConsumer):
    GROUP = "presence"

    async def connect(self):
        self.user = self.scope.get("user")
        await self.channel_layer.group_add(self.GROUP, self.channel_name)
        if self.user and not self.user.is_anonymous:
            await self.channel_layer.group_add(
                presence_user_group(self.user.id), self.channel_name)
        await self.accept()

        if self.user and not self.user.is_anonymous:
            _online_connections[self.channel_name] = self.user.id
            _online_users.add(self.user.id)

        await self._send_count()
        await self._broadcast_count()

    async def disconnect(self, close_code):
        uid = _online_connections.pop(self.channel_name, None)
        if uid is not None and uid not in _online_connections.values():
            _online_users.discard(uid)
        await self._broadcast_count()
        await self.channel_layer.group_discard(self.GROUP, self.channel_name)
        if self.user and not self.user.is_anonymous:
            await self.channel_layer.group_discard(
                presence_user_group(self.user.id), self.channel_name)

    async def presence_force_remove(self, event):
        """Sent on logout from another process."""
        if self.user and not self.user.is_anonymous:
            await _drop_local(self.user.id)
        await self.close()

    async def receive(self, text_data=None, bytes_data=None):
        pass

    async def _send_count(self):
        await self.send(text_data=json.dumps(
            {"type": "online_count", "count": len(_online_users)}))

    async def _broadcast_count(self):
        await self.channel_layer.group_send(self.GROUP, {
            "type": "presence_update",
            "data": {"type": "online_count", "count": len(_online_users)},
        })

    async def presence_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))