import json
import logging
from html import escape

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth import get_user_model
from django.utils import timezone

log = logging.getLogger(__name__)
User = get_user_model()

# In-memory set of online user IDs (per-process; works with single-worker or
# if you broadcast presence updates via the channel layer).
_online_users: set[int] = set()

# ── Helpers ──────────────────────────────────────

def notif_group_name(user_id: int) -> str:
    """Canonical channel-layer group name for a user's notifications."""
    return f"notifications_user_{user_id}"


async def push_notification(channel_layer, recipient_id: int, data: dict):
    """Send a notification payload to a user's personal notification group.
    Call this from any consumer or from a sync view via async_to_sync."""
    await channel_layer.group_send(
        notif_group_name(recipient_id),
        {"type": "send_notification", **data},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NotificationConsumer — personal WS for real-time
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """Each logged-in user connects to ws://.../ws/notifications/
    to receive real-time unread count updates and notification payloads."""

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.group_name = notif_group_name(self.user.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send current unread count on connect
        count = await self._unread_count()
        await self.send_json({"type": "unread_count", "count": count})

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Client can request a fresh unread count
        if content.get("type") == "get_unread_count":
            count = await self._unread_count()
            await self.send_json({"type": "unread_count", "count": count})

    # ── Handler for channel-layer group_send ──────
    async def send_notification(self, event):
        """Called via channel_layer.group_send with type='send_notification'."""
        await self.send_json({
            "type": "new_notification",
            "verb": event.get("verb", ""),
            "actor": event.get("actor", ""),
            "message": event.get("message", ""),
            "url": event.get("url", ""),
            "unread_count": event.get("unread_count", 0),
        })

    @database_sync_to_async
    def _unread_count(self):
        from .models import Notification
        return Notification.unread_count(self.user)


class PrivateChatConsumer(AsyncJsonWebsocketConsumer):
    """Async WebSocket consumer for 1-on-1 private chat between friends."""

    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.friend_username = self.scope["url_route"]["kwargs"]["username"]
        self.friend = await self._get_user(self.friend_username)

        if self.friend is None or not await self._are_friends():
            await self.close()
            return

        # Deterministic group name: private_<lower_id>_<higher_id>
        ids = sorted([self.user.pk, self.friend.pk])
        self.room_group = f"private_{ids[0]}_{ids[1]}"

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

        # Track online status
        _online_users.add(self.user.pk)

        # Mark unread message notifications from this friend as read
        # and push updated count to the user's notification WS
        await self._clear_message_notifications()
        await self._broadcast_my_unread()

        # Notify friend that we're online
        await self.channel_layer.group_send(self.room_group, {
            "type": "presence_update",
            "user_id": self.user.pk,
            "username": self.user.username,
            "online": True,
        })

    async def disconnect(self, close_code):
        if hasattr(self, "room_group"):
            _online_users.discard(self.user.pk)
            await self.channel_layer.group_send(self.room_group, {
                "type": "presence_update",
                "user_id": self.user.pk,
                "username": self.user.username,
                "online": False,
            })
            await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type", "")

        if msg_type == "chat_message":
            text = content.get("text", "").strip()
            if not text or len(text) > 4000:
                return
            # Sanitize
            safe_text = escape(text)
            # Save to DB
            message = await self._save_message(safe_text)
            # Broadcast to group
            await self.channel_layer.group_send(self.room_group, {
                "type": "chat_message",
                "message_id": message.pk,
                "sender": self.user.username,
                "sender_id": self.user.pk,
                "text": safe_text,
                "created_at": message.created_at.isoformat(),
            })

            # ── Push notification to recipient (if they are NOT in this chat room) ──
            # We check if the friend is in the private chat group; if not, create
            # a Notification and push via their personal notification channel.
            if self.friend.pk not in _online_users:
                notif = await self._create_message_notification(safe_text)
                await push_notification(self.channel_layer, self.friend.pk, {
                    "verb": "new_message",
                    "actor": self.user.username,
                    "message": f"{self.user.username} sent you a message",
                    "url": f"/chat/@{self.user.username}/",
                    "unread_count": notif["unread_count"],
                })

        elif msg_type == "mark_read":
            await self._mark_read()
            await self._clear_message_notifications()
            await self._broadcast_my_unread()

        elif msg_type == "typing":
            await self.channel_layer.group_send(self.room_group, {
                "type": "typing_indicator",
                "username": self.user.username,
                "user_id": self.user.pk,
            })

    # ── Group message handlers ───────────────────
    async def chat_message(self, event):
        await self.send_json({
            "type": "chat_message",
            "message_id": event["message_id"],
            "sender": event["sender"],
            "sender_id": event["sender_id"],
            "text": event["text"],
            "created_at": event["created_at"],
        })

    async def presence_update(self, event):
        await self.send_json({
            "type": "presence",
            "user_id": event["user_id"],
            "username": event["username"],
            "online": event["online"],
        })

    async def typing_indicator(self, event):
        # Don't echo typing back to the sender
        if event["user_id"] != self.user.pk:
            await self.send_json({
                "type": "typing",
                "username": event["username"],
            })

    # ── DB helpers (sync → async) ────────────────
    @database_sync_to_async
    def _get_user(self, username):
        return User.objects.filter(username=username).first()

    @database_sync_to_async
    def _are_friends(self):
        return self.user.is_friend(self.friend)

    @database_sync_to_async
    def _save_message(self, text):
        from .models import Conversation, Message
        convo = Conversation.get_or_create_for_users(self.user, self.friend)
        return Message.objects.create(
            conversation=convo,
            sender=self.user,
            text=text,
        )

    @database_sync_to_async
    def _mark_read(self):
        from .models import Conversation
        convo = Conversation.get_or_create_for_users(self.user, self.friend)
        convo.messages.filter(sender=self.friend, is_read=False).update(is_read=True)

    @database_sync_to_async
    def _create_message_notification(self, text):
        from .models import Notification
        Notification.objects.create(
            recipient=self.friend,
            actor=self.user,
            verb=Notification.Verb.NEW_MESSAGE,
        )
        return {"unread_count": Notification.unread_count(self.friend)}

    @database_sync_to_async
    def _clear_message_notifications(self):
        """Mark 'new_message' notifications from this friend as read."""
        from .models import Notification
        Notification.objects.filter(
            recipient=self.user,
            actor=self.friend,
            verb=Notification.Verb.NEW_MESSAGE,
            is_read=False,
        ).update(is_read=True)

    async def _broadcast_my_unread(self):
        """Push a fresh unread_count to the current user's notification channel."""
        from .models import Notification
        count = await database_sync_to_async(Notification.unread_count)(self.user)
        await push_notification(self.channel_layer, self.user.pk, {
            "verb": "_refresh",
            "actor": "",
            "message": "",
            "url": "",
            "unread_count": count,
        })
