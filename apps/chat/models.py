from django.conf import settings
from django.db import models


class FriendRequest(models.Model):
    """A directional friend request: sender → receiver."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_friend_requests",
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_friend_requests",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        # Prevent duplicate pending requests between same pair
        constraints = [
            models.UniqueConstraint(
                fields=["sender", "receiver"],
                condition=models.Q(status="pending"),
                name="unique_pending_request",
            ),
        ]

    def __str__(self):
        return f"{self.sender} → {self.receiver} ({self.status})"


class Conversation(models.Model):
    """A private 1-on-1 conversation between two users (friends only)."""

    user1 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations_as_user1",
    )
    user2 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations_as_user2",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Ensure user1.pk < user2.pk at save time to prevent duplicate pairs
        constraints = [
            models.UniqueConstraint(
                fields=["user1", "user2"],
                name="unique_conversation_pair",
            ),
        ]

    def __str__(self):
        return f"Conversation({self.user1} ↔ {self.user2})"

    @staticmethod
    def get_or_create_for_users(user_a, user_b):
        """Return the conversation between two users, creating if needed.
        Always stores the lower PK as user1 to enforce uniqueness."""
        u1, u2 = (user_a, user_b) if user_a.pk < user_b.pk else (user_b, user_a)
        convo, _ = Conversation.objects.get_or_create(user1=u1, user2=u2)
        return convo


class Message(models.Model):
    """A single chat message within a conversation."""

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_messages",
    )
    text = models.TextField(max_length=4000)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        # Truncate long messages to keep admin/shell output readable
        preview = self.text[:50] + "…" if len(self.text) > 50 else self.text
        return f"{self.sender} → {preview}"


class Notification(models.Model):
    """In-app notification (friend request, new message, etc.)."""

    class Verb(models.TextChoices):
        FRIEND_REQUEST = "friend_request", "Friend Request"
        NEW_MESSAGE = "new_message", "New Message"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="actions",
        help_text="The user who triggered this notification.",
    )
    verb = models.CharField(max_length=20, choices=Verb.choices)
    # Optional FK to a related object (e.g. FriendRequest PK or Conversation PK)
    target_id = models.PositiveIntegerField(null=True, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read", "-created_at"]),
        ]

    def __str__(self):
        # Safe version – won't crash if actor is somehow missing
        actor_name = getattr(self.actor, 'username', 'someone')
        return f"Notification({actor_name} → {self.recipient}: {self.verb})"

    # ── Queryset helpers (class-level) ──────────
    @classmethod
    def unread_count(cls, user):
        return cls.objects.filter(recipient=user, is_read=False).count()

    @classmethod
    def recent(cls, user, limit=10):
        return cls.objects.filter(recipient=user).select_related("actor")[:limit]

