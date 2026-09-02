from django.contrib import admin, messages
from django.utils import timezone
from .models import (
    Badge, EligibilityVerification, GauntletStanding, Match, PayoutConfirmation,
    PrizeClaim, Tournament, TournamentChatMessage,
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
    list_filter = ("status", "type", "game_type", "category", "time_control", "is_money_tournament")
    search_fields = ("name",)
    inlines = [ParticipantInline]

    fieldsets = (
        (None, {
            "fields": ("name", "description"),
        }),
        ("Format", {
            "fields": ("type", "game_type", "time_control", "category", "capacity", "rounds_total", "join_password"),
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
        ("Prize / Money Tournament", {
            "fields": (
                "is_money_tournament",
                "prize_amount",
                "prize_currency",
                "prize_structure",
                "payout_status",
                "terms_text",
                "terms_version",
            ),
            "description": (
                "Enable \"is_money_tournament\" to activate prize-pool mode. "
                "Entry will be restricted to Israeli residents (IP-based). "
                "Set terms_version to a non-empty string (e.g. \"1.0\") to "
                "require participants to accept terms before joining. "
                "prize_structure is an optional JSON list defining per-place payouts, e.g. "
                '[{\"place\": 1, \"label\": \"1st\", \"amount\": 60}, '
                '{\"place\": 2, \"label\": \"2nd\", \"amount\": 30}, '
                '{\"place\": 3, \"label\": \"3rd\", \"amount\": 10}]'
            ),
            "classes": ("collapse",),
        }),
    )

    class Media:
        js = ("admin/js/tournament_type_fields.js",)

    @admin.display(description="Entry")
    def entry_display(self, obj):
        if obj.is_money_tournament and obj.prize_amount:
            return f"💰 {obj.prize_amount} {obj.prize_currency}"
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
        "join_country_code", "geo_eligible", "terms_accepted_at",
        "paypal_email_display",
    )
    list_filter = (
        "tournament", "eliminated", "disqualified_for_sha_mismatch",
        "geo_eligible",
    )
    readonly_fields = (
        "join_ip", "join_country_code", "geo_eligible",
        "terms_accepted_at", "terms_version_accepted",
    )
    search_fields = ("user__username", "tournament__name")
    actions = ["run_manual_sha_check", "clear_paypal_email"]

    @admin.display(description="PayPal email")
    def paypal_email_display(self, obj):
        return obj.paypal_email or "—"

    @admin.action(description="Clear PayPal email (after payout confirmation)")
    def clear_paypal_email(self, request, queryset):
        updated = queryset.update(paypal_email="")
        self.message_user(
            request,
            f"Cleared PayPal email for {updated} participant(s).",
            level=messages.INFO,
        )

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


@admin.register(PrizeClaim)
class PrizeClaimAdmin(admin.ModelAdmin):
    list_display = (
        "tournament", "winner", "amount", "currency",
        "status", "created_at", "expires_at",
    )
    list_filter = ("status",)
    readonly_fields = ("claim_code", "created_at", "claimed_at", "paid_at", "paid_by_admin")
    search_fields = ("tournament__name", "winner__username")
    ordering = ("-created_at",)
    actions = ["mark_as_paid"]

    @admin.action(description="Mark selected as Paid")
    def mark_as_paid(self, request, queryset):
        now = timezone.now()
        paid = skipped = blocked = 0
        for claim in queryset:
            if claim.status != PrizeClaim.Status.CLAIMED:
                skipped += 1
                continue
            # Require winner's payout confirmation before marking paid.
            has_confirmation = PayoutConfirmation.objects.filter(
                tournament_entry__tournament=claim.tournament,
                tournament_entry__user=claim.winner,
                confirmed_by_user=True,
            ).exists()
            if not has_confirmation:
                blocked += 1
                continue
            claim.status = PrizeClaim.Status.PAID
            claim.paid_at = now
            claim.paid_by_admin = request.user
            claim.save(update_fields=["status", "paid_at", "paid_by_admin"])
            try:
                claim.tournament.payout_status = Tournament.PayoutStatus.PAID
                claim.tournament.save(update_fields=["payout_status"])
            except Exception:
                pass
            paid += 1
        if paid:
            self.message_user(request, f"Marked {paid} claim(s) as paid.", level=messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"{skipped} claim(s) skipped — only CLAIMED items can be marked as paid.",
                level=messages.WARNING,
            )
        if blocked:
            self.message_user(
                request,
                f"{blocked} claim(s) blocked — winner has not confirmed their payout address yet.",
                level=messages.ERROR,
            )

    @admin.display(description="Current")
    def current_short(self, obj):
        return (obj.current_sha[:12] + "...") if obj.current_sha else "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EligibilityVerification)
class EligibilityVerificationAdmin(admin.ModelAdmin):
    list_display = (
        "tournament_entry", "status", "verification_method",
        "verified_at", "verified_by",
    )
    list_filter = ("status",)
    search_fields = (
        "tournament_entry__user__username",
        "tournament_entry__tournament__name",
    )
    ordering = ("-tournament_entry__joined_at",)
    readonly_fields = ("tournament_entry",)
    # No FileField or file upload widget anywhere in this admin.
    fields = (
        "tournament_entry",
        "status",
        "verification_method",
        "verified_at",
        "verified_by",
        "notes",
    )

    def save_model(self, request, obj, form, change):
        if obj.status in (
            EligibilityVerification.Status.VERIFIED,
            EligibilityVerification.Status.REJECTED,
        ) and not obj.verified_by:
            obj.verified_by = request.user
        if obj.status in (
            EligibilityVerification.Status.VERIFIED,
            EligibilityVerification.Status.REJECTED,
        ) and not obj.verified_at:
            obj.verified_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(PayoutConfirmation)
class PayoutConfirmationAdmin(admin.ModelAdmin):
    list_display = (
        "tournament_entry", "paypal_email_snapshot",
        "confirmed_by_user", "confirmed_at",
    )
    list_filter = ("confirmed_by_user",)
    search_fields = (
        "tournament_entry__user__username",
        "tournament_entry__tournament__name",
        "paypal_email_snapshot",
    )
    ordering = ("-confirmed_at",)
    readonly_fields = (
        "tournament_entry", "paypal_email_snapshot",
        "confirmed_by_user", "confirmed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
