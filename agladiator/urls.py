# ──────────────────────────────────────────────
# mysite/urls.py — Root URL configuration
# ──────────────────────────────────────────────
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/static/img/favicon.ico')),
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("users/", include("apps.users.urls")),
    path("users/", include("django.contrib.auth.urls")),  # password_reset, etc.
    path("tournaments/", include("apps.tournaments.urls")),
    path("games/", include("apps.games.urls")),
    # chat app removed (apps.chat does not exist)
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
