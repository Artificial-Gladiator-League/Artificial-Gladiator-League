from django.contrib import admin
from .models import Comment, Game


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = (
        "id", "white", "black", "result", "status", "winner",
        "time_control", "is_tournament_game", "timestamp",
    )
    list_filter = ("result", "status", "time_control", "is_tournament_game")
    search_fields = ("white__username", "black__username")
    readonly_fields = (
        "elo_change_white", "elo_change_black",
        "current_fen", "move_list", "pgn",
    )


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "game", "parent", "short_content", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "content")
    raw_id_fields = ("user", "game", "parent")

    @admin.display(description="Content")
    def short_content(self, obj):
        return obj.content[:80] + "…" if len(obj.content) > 80 else obj.content
