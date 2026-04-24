from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django import forms

from .models import CustomUser, GDPRRequest, UserGameModel


class UserGameModelForm(forms.ModelForm):
    """Admin form that auto-populates endpoint fields so MySQL NOT NULL is satisfied."""

    class Meta:
        model = UserGameModel
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        repo = cleaned.get("hf_model_repo_id") or ""
        repo_slug = repo.split("/")[-1] if repo else "model"

        user = cleaned.get("user") or getattr(self.instance, "user", None)
        username = user.username if user else "unknown"

        # Auto-populate endpoint_id if blank / null
        if not cleaned.get("hf_inference_endpoint_id"):
            cleaned["hf_inference_endpoint_id"] = f"{username}-{repo_slug}"

        # Auto-populate endpoint_name if blank
        if not cleaned.get("hf_inference_endpoint_name"):
            cleaned["hf_inference_endpoint_name"] = repo_slug

        # Default status to 'pending' if blank
        if not cleaned.get("hf_inference_endpoint_status"):
            cleaned["hf_inference_endpoint_status"] = "pending"

        return cleaned

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


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = (
        "username", "elo", "wins", "losses", "draws",
        "total_games", "current_streak", "ai_name",
    )
    list_filter = ("is_staff", "is_active")
    search_fields = ("username", "ai_name")
    ordering = ("-elo",)

    # Extend the default UserAdmin fieldsets
    fieldsets = UserAdmin.fieldsets + (
        ("AI Bot", {
            "fields": ("ai_name", "hf_model_repo_id"),
        }),
        ("Stats", {
            "fields": ("elo", "wins", "losses", "draws", "total_games", "current_streak"),
        }),
    )
    add_fieldsets = UserAdmin.add_fieldsets
    inlines = [UserGameModelInline]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk:
            for fname in _ADMIN_LOCKED_FIELDS:
                if fname not in readonly:
                    readonly.append(fname)
        return readonly


@admin.register(GDPRRequest)
class GDPRRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "request_type", "status", "created_at", "resolved_at")
    list_filter = ("request_type", "status")
    search_fields = ("user__username",)
    readonly_fields = ("user", "request_type", "reason", "created_at")


@admin.register(UserGameModel)
class UserGameModelAdmin(admin.ModelAdmin):
    form = UserGameModelForm
    list_display = ("user", "game_type", "hf_model_repo_id", "model_integrity_ok", "rated_games_played")
    list_filter = ("game_type", "model_integrity_ok")
    search_fields = ("user__username", "hf_model_repo_id")
    readonly_fields = (
        "original_model_commit_sha", "last_known_commit_id",
        "approved_full_sha", "pinned_at",
    )
