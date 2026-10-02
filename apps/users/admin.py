from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django import forms
from django.template.response import TemplateResponse

from .countries import country_choices, is_valid_country
from .models import CountryChangeLog, CustomUser, GDPRRequest, UserGameModel


class ChangeCountryForm(forms.Form):
    country = forms.ChoiceField(choices=[("", "— select —")] + list(country_choices()))
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Required. Saved in the country change log.",
    )

    def clean_country(self):
        code = (self.cleaned_data.get("country") or "").upper()
        if not is_valid_country(code):
            raise forms.ValidationError("Choose a country.")
        return code

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("A reason is required.")
        return reason


class UserGameModelForm(forms.ModelForm):
    class Meta:
        model = UserGameModel
        fields = "__all__"

# Fields permanently locked after registration.
_ADMIN_LOCKED_FIELDS = (
    "username", "ai_name",
)


class UserGameModelInline(admin.TabularInline):
    model = UserGameModel
    extra = 0
    readonly_fields = (
        "original_model_commit_sha", "last_known_commit_id",
        "approved_full_sha", "pinned_at",
    )


class CountryChangeLogInline(admin.TabularInline):
    model = CountryChangeLog
    extra = 0
    fk_name = "user"
    can_delete = False
    readonly_fields = ("changed_at", "changed_by", "old_country", "new_country", "reason")
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username", "elo", "wins", "losses", "draws",
        "total_games", "current_streak", "ai_name", "country",
    )
    list_filter = ("is_staff", "is_active", "country_locked")
    search_fields = ("username", "ai_name")
    ordering = ("-elo",)
    actions = ["change_locked_country"]

    # Extend the default UserAdmin fieldsets
    fieldsets = UserAdmin.fieldsets + (
        ("AI Bot", {
            "fields": ("ai_name", "hf_model_repo_id"),
        }),
        ("Stats", {
            "fields": ("elo", "wins", "losses", "draws", "total_games", "current_streak"),
        }),
        ("Residency", {
            "fields": ("country", "country_set_at", "country_locked", "show_flag"),
            "description": (
                "Read-only here. To change a saved country use the list action "
                "\u201cChange locked country\u201d, which asks for a reason and logs the change."
            ),
        }),
    )
    add_fieldsets = UserAdmin.add_fieldsets
    inlines = [UserGameModelInline, CountryChangeLogInline]

    _COUNTRY_READONLY = ("country", "country_set_at", "country_locked", "show_flag")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        for fname in self._COUNTRY_READONLY:
            if fname not in readonly:
                readonly.append(fname)
        if obj and obj.pk:
            for fname in _ADMIN_LOCKED_FIELDS:
                if fname not in readonly:
                    readonly.append(fname)
        return readonly

    @admin.action(description="Change locked country (reason required, logged)", permissions=["change"])
    def change_locked_country(self, request, queryset):
        if "apply" in request.POST:
            form = ChangeCountryForm(request.POST)
            if form.is_valid():
                changed = 0
                for user in queryset:
                    user.change_country_by_staff(form.cleaned_data["country"], request.user, form.cleaned_data["reason"])
                    changed += 1
                self.message_user(request, f"Country changed for {changed} user(s) and logged.", level=messages.SUCCESS)
                return None
        else:
            form = ChangeCountryForm()
        return TemplateResponse(request, "admin/users/change_country_form.html", {
            **self.admin_site.each_context(request),
            "title": "Change locked country",
            "users": queryset,
            "selected": list(queryset.values_list("pk", flat=True)),
            "action": "change_locked_country",
            "form": form,
            "opts": self.model._meta,
        })


@admin.register(CountryChangeLog)
class CountryChangeLogAdmin(admin.ModelAdmin):
    list_display = ("user", "old_country", "new_country", "changed_by", "changed_at")
    search_fields = ("user__username", "reason")
    readonly_fields = ("user", "changed_by", "changed_at", "old_country", "new_country", "reason")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GDPRRequest)
class GDPRRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "request_type", "status", "created_at", "resolved_at")
    list_filter = ("request_type", "status")
    search_fields = ("user__username",)
    readonly_fields = ("user", "request_type", "reason", "created_at")


@admin.register(UserGameModel)
class UserGameModelAdmin(admin.ModelAdmin):
    form = UserGameModelForm
    list_display = ("user", "game_type", "hf_model_repo_id", "model_integrity_ok", "rated_games_played", "rated_games_since_revalidation")
    list_filter = ("game_type", "model_integrity_ok")
    search_fields = ("user__username", "hf_model_repo_id")
    readonly_fields = (
        "original_model_commit_sha", "last_known_commit_id",
        "approved_full_sha", "pinned_at",
    )
    actions = ["reset_readiness_counter"]

    @admin.action(description="Reset tournament readiness counter to 0 (repo changed)")
    def reset_readiness_counter(self, request, queryset):
        updated = queryset.update(
            rated_games_since_revalidation=0,
            model_integrity_ok=False,
        )
        self.message_user(request, f"Reset readiness counter for {updated} model(s).")
