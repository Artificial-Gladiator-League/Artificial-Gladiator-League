# ──────────────────────────────────────────────
# mysite/urls.py — Root URL configuration
# ──────────────────────────────────────────────
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy
from django.views.generic import RedirectView

# Derive domain/protocol from SITE_URL so reset emails never expose request.get_host().
_RESET_DOMAIN = settings.SITE_URL.replace("https://", "").replace("http://", "").rstrip("/")
_RESET_PROTOCOL = "https" if settings.SITE_URL.startswith("https") else "http"

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/static/img/favicon.ico')),
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("users/", include("apps.users.urls")),
    # Password reset — explicit so domain comes from SITE_URL, not request.get_host()
    path("users/password_reset/", auth_views.PasswordResetView.as_view(
        email_template_name="registration/password_reset_email.html",
        subject_template_name="registration/password_reset_subject.txt",
        extra_email_context={"domain": _RESET_DOMAIN, "protocol": _RESET_PROTOCOL},
    ), name="password_reset"),
    path("users/password_reset/done/", auth_views.PasswordResetDoneView.as_view(),
         name="password_reset_done"),
    path("users/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        success_url=reverse_lazy("password_reset_complete"),
    ), name="password_reset_confirm"),
    path("users/reset/done/", auth_views.PasswordResetCompleteView.as_view(),
         name="password_reset_complete"),
    path("tournaments/", include("apps.tournaments.urls")),
    path("games/", include("apps.games.urls")),
    # chat app removed (apps.chat does not exist)
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
