from django.contrib import admin
from .models import Badge, GauntletStanding, Match, Tournament, TournamentChatMessage, TournamentParticipant


class ParticipantInline(admin.TabularInline):
    model = TournamentParticipant
    extra = 0
    readonly_fields = ("seed", "current_round", "eliminated", "eliminated_in_round")


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    list_display = (
        "name", "type", "game_type", "category", "time_control",
        "status", "current_round",
        "participant_count", "capacity", "rounds_total",
        "prize_pool", "entry_display", "start_time",
    )
    list_filter = ("status", "type", "game_type", "category", "time_control")
    search_fields = ("name",)
    inlines = [ParticipantInline]

    fieldsets = (
        (None, {
            "fields": ("name", "description"),
        }),
        ("Format", {
            "fields": ("type", "game_type", "time_control", "category", "capacity", "rounds_total"),
            "description": (
                "Choose any tournament type. Capacity and rounds are auto-set "
                "based on type but can be overridden. QA tournaments lock "
                "capacity to 2 and rounds to 1."
            ),
        }),
        ("Schedule & Status", {
            "fields": ("start_time", "status", "current_round"),
        }),
        ("Prizes", {
            "fields": ("prize_pool", "rollover_amount", "champion"),
        }),
    )

    class Media:
        js = ("admin/js/tournament_type_fields.js",)

    @admin.display(description="Entry")
    def entry_display(self, obj):
        return "Free"


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = (
        "tournament", "round_num", "bracket_position",
        "player1", "player2", "result", "match_status",
        "is_armageddon", "winner",
        "elo_change_p1", "elo_change_p2", "timestamp",
    )
    list_filter = ("tournament", "round_num", "match_status", "is_armageddon", "result")
    search_fields = ("player1__username", "player2__username")
    readonly_fields = ("elo_change_p1", "elo_change_p2")


@admin.register(TournamentParticipant)
class TournamentParticipantAdmin(admin.ModelAdmin):
    list_display = ("user", "tournament", "seed", "current_round", "eliminated")
    list_filter = ("tournament", "eliminated")


@admin.register(GauntletStanding)
class GauntletStandingAdmin(admin.ModelAdmin):
    list_display = ("tournament", "user", "rank", "score", "wins", "draws", "losses", "buchholz")
    list_filter = ("tournament",)
    search_fields = ("user__username",)
    ordering = ("tournament", "rank")


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    list_display = ("user", "badge_type", "label", "tournament", "awarded_at")
    list_filter = ("badge_type",)
    search_fields = ("user__username", "label")
    ordering = ("-awarded_at",)


@admin.register(TournamentChatMessage)
class TournamentChatMessageAdmin(admin.ModelAdmin):
    list_display = ("tournament", "user", "content_short", "created_at")
    list_filter = ("tournament",)
    search_fields = ("user__username", "content")
    ordering = ("-created_at",)
    raw_id_fields = ("tournament", "user")

    @admin.display(description="Message")
    def content_short(self, obj):
        return obj.content[:80]
