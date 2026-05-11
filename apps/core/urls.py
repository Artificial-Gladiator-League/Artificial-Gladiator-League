from django.urls import path
from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("privacy/", views.privacy, name="privacy"),
    path("about/", views.about, name="about"),
    path("terms/", views.terms, name="terms"),
    path("accessibility/", views.accessibility, name="accessibility"),
    path("cookies/", views.cookies, name="cookies"),
    path("how-to-upload/", views.how_to_upload, name="how_to_upload"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    path("api/leaderboard/", views.leaderboard_json, name="leaderboard_json"),
    path("ws/presence/", views.presence_view, name="presence"),
]
