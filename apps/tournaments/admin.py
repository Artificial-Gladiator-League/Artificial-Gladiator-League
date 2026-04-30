from django.contrib import admin, messages
from .models import (
    Badge, GauntletStanding, Match, Tournament, TournamentChatMessage,
    TournamentParticipant, TournamentShaCheck,
)


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
        "entry_display", "start_time",
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
        ("Champion", {
            "fields": ("champion",),
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
    list_display = (
        "user", "tournament", "seed", "current_round",
        "eliminated", "disqualified_for_sha_mismatch",
        "round_pinned_sha_short", "round_pinned_at",
    )
    list_filter = (
        "tournament", "eliminated", "disqualified_for_sha_mismatch",
    )
    search_fields = ("user__username", "tournament__name")
    actions = ["run_manual_sha_check"]

    @admin.display(description="Pinned SHA")
    def round_pinned_sha_short(self, obj):
        return (obj.round_pinned_sha[:12] + "...") if obj.round_pinned_sha else "—"

    @admin.action(description="Run anti-cheat SHA check now")
    def run_manual_sha_check(self, request, queryset):
        from apps.tournaments.sha_audit import perform_sha_check
        passed = failed = errors = 0
        for p in queryset.select_related("tournament", "user"):
            row = perform_sha_check(p, context="manual")
            if row is None:
                errors += 1
            elif row.result == TournamentShaCheck.Result.PASS:
                passed += 1
            elif row.result == TournamentShaCheck.Result.FAIL:
                failed += 1
            else:
                errors += 1
        self.message_user(
            request,
            f"SHA audit: {passed} pass, {failed} fail, {errors} skipped/error.",
            level=messages.WARNING if failed else messages.INFO,
        )


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


@admin.register(TournamentShaCheck)
class TournamentShaCheckAdmin(admin.ModelAdmin):
    list_display = (
        "checked_at", "tournament", "round_num", "user",
        "repo_id", "expected_short", "current_short",
        "result", "context", "action_taken",
    )
    list_filter = ("result", "context", "tournament", "round_num", "game_type")
    search_fields = (
        "user__username", "repo_id",
        "expected_sha", "current_sha", "tournament__name",
    )
    readonly_fields = (
        "tournament", "participant", "user", "round_num", "game_type",
        "repo_id", "expected_sha", "current_sha", "result", "context",
        "action_taken", "error_message", "checked_at",
    )
    date_hierarchy = "checked_at"
    ordering = ("-checked_at",)

    @admin.display(description="Expected")
    def expected_short(self, obj):
        return (obj.expected_sha[:12] + "...") if obj.expected_sha else "—"

    @admin.display(description="Current")
    def current_short(self, obj):
        return (obj.current_sha[:12] + "...") if obj.current_sha else "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
