from django.urls import path
from django.views.generic import RedirectView
from . import views

app_name = "tournaments"

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/img/favicon.ico')),
    path("", views.tournament_list, name="list"),
    path("<int:pk>/", views.tournament_detail, name="detail"),
    path("<int:pk>/join/", views.join_tournament, name="join"),
    path("<int:pk>/leave/", views.leave_tournament, name="leave"),
    path("<int:pk>/ready/", views.ready_tournament, name="ready"),
    path("<int:pk>/match/<int:match_id>/", views.live_match, name="live_match"),
    path("<int:pk>/match/<int:match_id>/resign/", views.resign_match, name="resign"),

    # Live Chat — REMOVED
    # path("<int:pk>/chat/messages/", views.chat_messages, name="chat_messages"),
    # path("<int:pk>/chat/send/", views.chat_send, name="chat_send"),

    # Gladiator Gauntlet
    path("gauntlet/", views.gauntlet_detail, name="gauntlet"),
    path("gauntlet/<int:pk>/", views.gauntlet_detail, name="gauntlet_by_pk"),
    path("gauntlet/<int:pk>/standings/", views.gauntlet_standings_partial, name="gauntlet_standings_partial"),
]
