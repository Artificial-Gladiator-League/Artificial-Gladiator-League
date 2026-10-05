from django import forms
from django.contrib import admin, messages
from django.db.models import Q
from django.forms import Textarea
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html
from . import payouts
from .forms import CountryCodesField
from .profile_country import AGREE, MISMATCH, country_agreement
from .models import (
    Badge, EligibilityVerification, GauntletStanding, Match, PayoutConfirmation,
    PrizeClaim, PrizeClaimEvent, Tournament, TournamentChatMessage,
    TournamentParticipant, TournamentShaCheck, TournamentTerms,
)
from .sensitive import validate_no_sensitive_data


@admin.register(TournamentTerms)
class TournamentTermsAdmin(admin.ModelAdmin):
    list_display = (
        "title", "slug", "version", "is_active", "requires_age_18",
        "requires_israeli_residency", "has_prize", "acceptances_display", "created_at",
    )
    list_filter = ("is_active", "has_prize", "requires_age_18", "requires_israeli_residency")
    search_fields = ("title", "slug", "version")
    readonly_fields = ("created_at",)
    # "Save as new" is the intended way to publish a new version.
    save_as = True
    fieldsets = (
        (None, {"fields": ("slug", "title", "version", "is_active", "created_at")}),
        ("Requirements", {"fields": ("requires_age_18", "requires_israeli_residency", "has_prize")}),
        ("Body", {"fields": ("body",)}),
    )
    _CONTENT_FIELDS = ("title", "body", "requires_age_18", "requires_israeli_residency", "has_prize")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "body":
            kwargs["widget"] = Textarea(attrs={
                "rows": 40, "cols": 140,
                "style": "width:100%;font-family:monospace;",
            })
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description="Acceptances")
    def acceptances_display(self, obj):
        return obj.acceptance_count()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if request.method == "GET":
            obj = self.get_object(request, object_id)
            count = obj.acceptance_count() if obj else 0
            if count:
                self.message_user(
                    request,
                    f"{count} player(s) have already accepted {obj}. Do not change its "
                    f"wording: change the version number and use \"Save as new\" to "
                    f"publish a new version instead.",
                    level=messages.WARNING,
                )
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        # form.initial holds the saved slug/version; obj already carries the edited ones.
        accepted = change and TournamentParticipant.objects.filter(
            accepted_terms_slug=form.initial.get("slug"),
            terms_version_accepted=form.initial.get("version"),
        ).exists()
        if accepted and any(f in form.changed_data for f in self._CONTENT_FIELDS):
            self.message_user(
                request,
                f"Edited the wording of {obj}, which players have already accepted. "
                f"Prefer publishing a new version.",
                level=messages.WARNING,
            )
        super().save_model(request, obj, form, change)


class ParticipantInline(admin.TabularInline):
    model = TournamentParticipant
    extra = 0
    readonly_fields = ("seed", "current_round", "eliminated", "eliminated_in_round", "profile_country_code")


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    list_display = (
        "name", "type", "game_type", "category", "time_control",
        "status", "current_round",
        "participant_count", "capacity", "rounds_total",
        "countries_display", "prize_display", "start_time",
    )
    list_filter = ("status", "type", "game_type", "category", "time_control", "is_money_tournament")
    search_fields = ("name",)
    inlines = [ParticipantInline]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "prize_on_hold" in form.changed_data:
            payouts.sync_for_tournament(obj)

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
                "allowed_countries",
                "prize_amount",
                "prize_currency",
                "prize_structure",
                "payout_status",
                "payout_method",
                "payout_instructions",
                "claim_deadline_days",
                "prize_on_hold",
                "prize_hold_reason",
                "verification_documents",
                "terms",
                "terms_text",
                "terms_version",
            ),
            "description": (
                "Enable \"is_money_tournament\" to activate prize-pool mode. "
                "Entry is restricted to residents of the countries in allowed_countries "
                "(IP-based). Leave allowed_countries empty to open the tournament to all countries. "
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

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "allowed_countries":
            return CountryCodesField(label=db_field.verbose_name.capitalize(), help_text=db_field.help_text)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description="Countries")
    def countries_display(self, obj):
        if obj.is_open_to_all:
            return "All countries"
        return ", ".join(obj.allowed_country_codes)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        # The add form opens as a Gauntlet (the model's default type), which is open to all countries;
        # the admin script switches the field for other types.
        if initial.get("type", Tournament.Type.GAUNTLET) == Tournament.Type.GAUNTLET:
            initial.setdefault("allowed_countries", [])
        return initial

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Active records only, plus the one already linked so the tournament still saves.
        field = form.base_fields.get("terms")
        if field is not None:
            active = Q(is_active=True)
            if obj is not None and obj.terms_id:
                active |= Q(pk=obj.terms_id)
            field.queryset = TournamentTerms.objects.filter(active)
        return form

    @admin.display(description="Prize")
    def prize_display(self, obj):
        if obj.is_money_tournament and obj.prize_amount:
            return f"{obj.prize_amount} {obj.prize_currency}"
        return "\u2014"


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


class CountryAgreementFilter(admin.SimpleListFilter):
    """Profile country vs join IP vs declared residency: staff-visible signal only."""

    title = "profile / IP / declared countries"
    parameter_name = "country_agreement"

    def lookups(self, request, model_admin):
        return (("mismatch", "Disagree"), ("ok", "Agree"), ("n/a", "Not enough data"))

    def queryset(self, request, queryset):
        wanted = self.value()
        if wanted not in ("mismatch", "ok", "n/a"):
            return queryset
        ids = [p.pk for p in queryset.only(
            "profile_country_code", "join_country_code", "declared_residency_countries",
        ) if country_agreement(p)[0] == wanted]
        return queryset.filter(pk__in=ids)


@admin.register(TournamentParticipant)
class TournamentParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "user", "tournament", "seed", "current_round",
        "eliminated", "disqualified_for_sha_mismatch",
        "round_pinned_sha_short", "round_pinned_at",
        "profile_country_code", "join_country_code", "declared_residency_countries", "country_check",
        "geo_eligible", "terms_accepted_at",
        "paypal_email_display",
    )
    list_filter = (
        "tournament", "eliminated", "disqualified_for_sha_mismatch",
        "geo_eligible", CountryAgreementFilter,
    )
    readonly_fields = (
        "join_ip", "join_country_code", "declared_residency_countries", "profile_country_code",
        "country_check", "geo_eligible",
        "terms_accepted_at", "terms_version_accepted", "accepted_terms_slug",
    )
    search_fields = ("user__username", "tournament__name")
    actions = ["run_manual_sha_check", "clear_paypal_email"]

    @admin.display(description="Countries (profile / IP / declared)")
    def country_check(self, obj):
        state, detail = country_agreement(obj)
        if state == MISMATCH:
            return format_html('<strong style="color:#b02a2a">MISMATCH</strong> ({})', detail)
        if state == AGREE:
            return format_html("agree ({})", detail)
        return format_html("\u2014 ({})", detail)

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


class ReasonForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "cols": 70}),
        validators=[validate_no_sensitive_data],
        help_text="Required. Recorded with your name and the time. Never enter document numbers.",
    )


class NoteForm(forms.Form):
    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "cols": 70}),
        validators=[validate_no_sensitive_data],
    )


class PaymentRecordForm(forms.Form):
    amount_paid = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False)
    tax_withheld = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False)
    payment_reference = forms.CharField(
        max_length=100, required=False, validators=[validate_no_sensitive_data],
        help_text="Free text, e.g. a transfer reference. Never an account or UPI number.",
    )


class PrizeClaimEventInline(admin.TabularInline):
    model = PrizeClaimEvent
    extra = 0
    can_delete = False
    fields = ("at", "kind", "by", "reason")
    readonly_fields = ("at", "kind", "by", "reason")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PrizeClaim)
class PrizeClaimAdmin(admin.ModelAdmin):
    list_display = (
        "tournament", "winner", "amount", "currency",
        "status", "created_at", "expires_at",
    )
    list_filter = ("status", "payout_method")
    readonly_fields = ("claim_code", "created_at", "claimed_at", "paid_at", "paid_by_admin")
    search_fields = ("tournament__name", "winner__username")
    ordering = ("-created_at",)
    inlines = [PrizeClaimEventInline]
    actions = [
        "mark_as_paid", "block_claims", "unblock_claims",
        "record_contact", "record_payment_details", "add_note",
    ]

    @admin.action(description="Mark selected as Paid")
    def mark_as_paid(self, request, queryset):
        now = timezone.now()
        paid = skipped = blocked = 0
        refused = []
        for claim in queryset.select_related("tournament", "winner"):
            if claim.status != PrizeClaim.Status.CLAIMED:
                skipped += 1
                continue
            tournament = claim.tournament
            label = f"Claim #{claim.pk} ({tournament.name})"
            if tournament.requires_legal_check:
                # Not Israel-only: every step must be done, in order.
                blocker = payouts.payout_blocker(claim)
                if blocker:
                    refused.append(f"{label}: {blocker.admin_message}.")
                    continue
            else:
                # Israel-only: the original check, plus the opt-in hold and non-PayPal contact step.
                blocker = payouts.payout_blocker(claim)
                if blocker and blocker.code in ("hold", "contacted"):
                    refused.append(f"{label}: {blocker.admin_message}.")
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
        for line in refused:
            self.message_user(request, line, level=messages.ERROR)

    # ── Actions with an intermediate form ────────────────────────────────────

    def _form_action(self, request, queryset, *, action, title, form_class, apply, intro=""):
        """Show *form_class* for the selected claims; on a valid POST run apply(claim, cleaned) on each.

        apply() raises payouts.ActionRefused to refuse one claim; the reason is shown to staff.
        """
        if "apply" in request.POST:
            form = form_class(request.POST)
            if form.is_valid():
                done = 0
                for claim in queryset:
                    try:
                        apply(claim, form.cleaned_data)
                        done += 1
                    except payouts.ActionRefused as exc:
                        self.message_user(
                            request, f"Claim #{claim.pk} ({claim.tournament.name}): {exc}.",
                            level=messages.ERROR,
                        )
                if done:
                    self.message_user(request, f"{title}: {done} claim(s) updated.", level=messages.SUCCESS)
                return None
        else:
            form = form_class()
        return TemplateResponse(request, "admin/tournaments/claim_action_form.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": title,
            "intro": intro,
            "form": form,
            "action": action,
            "claims": queryset,
            "selected": request.POST.getlist(admin.helpers.ACTION_CHECKBOX_NAME),
        })

    @admin.action(description="Block selected (reason required)")
    def block_claims(self, request, queryset):
        return self._form_action(
            request, queryset, action="block_claims", title="Block claims", form_class=ReasonForm,
            intro="Blocking stops the claim and the payout and pauses its expiry clock. "
                  "The winner only sees that the prize is on hold.",
            apply=lambda claim, data: payouts.block_claim(claim, request.user, data["reason"]),
        )

    @admin.action(description="Unblock selected (reason required)")
    def unblock_claims(self, request, queryset):
        return self._form_action(
            request, queryset, action="unblock_claims", title="Unblock claims", form_class=ReasonForm,
            apply=lambda claim, data: payouts.unblock_claim(claim, request.user, data["reason"]),
        )

    @admin.action(description="Record: payment details requested")
    def record_contact(self, request, queryset):
        done = 0
        for claim in queryset.select_related("tournament"):
            try:
                payouts.record_contact(claim, request.user)
                done += 1
            except payouts.ActionRefused as exc:
                self.message_user(
                    request, f"Claim #{claim.pk} ({claim.tournament.name}): {exc}.", level=messages.ERROR,
                )
        if done:
            self.message_user(request, f"Recorded the contact step for {done} claim(s).", level=messages.SUCCESS)

    @admin.action(description="Record payment details (amount, tax, reference)")
    def record_payment_details(self, request, queryset):
        return self._form_action(
            request, queryset, action="record_payment_details", title="Record payment details",
            form_class=PaymentRecordForm,
            intro="Record only; nothing is calculated. Never enter bank, UPI or document numbers.",
            apply=lambda claim, data: payouts.record_payment(
                claim, request.user, data["amount_paid"], data["tax_withheld"], data["payment_reference"],
            ),
        )

    @admin.action(description="Add a note")
    def add_note(self, request, queryset):
        return self._form_action(
            request, queryset, action="add_note", title="Add a note", form_class=NoteForm,
            intro="Never enter bank, UPI, PAN or document numbers.",
            apply=lambda claim, data: payouts.add_note(claim, request.user, data["note"]),
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
    readonly_fields = ("tournament_entry", "verified_at", "verified_by")
    # No FileField or file upload widget anywhere in this admin.
    fields = (
        "tournament_entry",
        "status",
        "verification_method",
        "verified_at",
        "verified_by",
        "notes",
    )

    def get_form(self, request, obj=None, **kwargs):
        base = super().get_form(request, obj, **kwargs)
        reviewer = request.user

        class StampedForm(base):
            """Stamp who and when before model validation, so 'verified' is always attributable."""

            def clean(self):
                cleaned = super().clean()
                status = cleaned.get("status")
                inst = self.instance
                decided = (EligibilityVerification.Status.VERIFIED, EligibilityVerification.Status.REJECTED)
                if status in decided:
                    if "status" in self.changed_data or not inst.verified_by_id or not inst.verified_at:
                        inst.verified_by = reviewer
                        inst.verified_at = timezone.now()
                elif status == EligibilityVerification.Status.PENDING:
                    inst.verified_by = None
                    inst.verified_at = None
                return cleaned

        return StampedForm

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        payouts.sync_for_entry(obj.tournament_entry)


@admin.register(PayoutConfirmation)
class PayoutConfirmationAdmin(admin.ModelAdmin):
    list_display = (
        "tournament_entry", "payout_method", "paypal_email_snapshot",
        "confirmed_by_user", "confirmed_at", "contacted_at",
    )
    list_filter = ("confirmed_by_user", "payout_method")
    search_fields = (
        "tournament_entry__user__username",
        "tournament_entry__tournament__name",
        "paypal_email_snapshot",
        "contact_email_snapshot",
    )
    ordering = ("-confirmed_at",)
    readonly_fields = (
        "tournament_entry", "paypal_email_snapshot", "payout_method", "contact_email_snapshot",
        "terms_slug", "terms_version", "contacted_at", "contacted_by",
        "confirmed_by_user", "confirmed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
