import json

from channels.generic.websocket import AsyncWebsocketConsumer


def notif_group_name(user_id: int) -> str:
    """Return the channel group name for a user's notifications."""
    return f"notifications_{user_id}"


class NotificationConsumer(AsyncWebsocketConsumer):
    """Simple notifications WebSocket consumer.

    - Authenticated users join a per-user group so server-side code can
      push notifications via the channel layer using `notif_group_name(user_id)`.
    - Sends an initial `unread_count` (0) on connect to satisfy the UI.
    - Handles `send_notification` group events and forwards them to clients.
    """

    async def connect(self):
        self.user = self.scope.get("user")
        self.group_name = None
        if self.user and not getattr(self.user, "is_anonymous", True):
            self.group_name = notif_group_name(self.user.pk)
            await self.channel_layer.group_add(self.group_name, self.channel_name)

        await self.accept()

        # Send an initial unread count (placeholder: 0).  Real implementation
        # can query a Notification model if available.
        await self.send(text_data=json.dumps({"type": "unread_count", "count": 0}))

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # No client-initiated messages expected for notifications.
        return

    async def send_notification(self, event):
        # Event shape is left intentionally permissive — forward relevant
        # fields to the browser.
        payload = {
            "type": "new_notification",
            "verb": event.get("verb"),
            "actor": event.get("actor"),
            "message": event.get("message"),
            "url": event.get("url"),
            "unread_count": event.get("unread_count", 0),
        }
        await self.send(text_data=json.dumps(payload))
