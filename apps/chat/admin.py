from django.contrib import admin
from .models import Conversation, FriendRequest, Message, Notification


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ("sender", "receiver", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("sender__username", "receiver__username")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("user1", "user2", "created_at")
    search_fields = ("user1__username", "user2__username")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient", "actor", "verb", "is_read", "created_at")
    list_filter = ("verb", "is_read")
    search_fields = ("recipient__username", "actor__username")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("sender", "conversation", "text_preview", "is_read", "created_at")
    list_filter = ("is_read",)

    @admin.display(description="Text")
    def text_preview(self, obj):
        return obj.text[:60]
