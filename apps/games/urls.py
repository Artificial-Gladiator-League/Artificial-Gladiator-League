from django.urls import path
from django.views.generic import RedirectView
from . import views

app_name = "games"

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url='/img/favicon.ico')),
    path("lobby/", views.lobby, name="lobby"),
    path("create/", views.create_game, name="create_game"),
    path("create-lobby/", views.create_lobby_game, name="create_lobby_game"),
    path("<int:game_id>/join/", views.join_game, name="join_game"),
    path("<int:game_id>/cancel/", views.cancel_game, name="cancel_game"),
    path("<int:game_id>/leave/", views.leave_game, name="leave_game"),
    path("<int:game_id>/spectate/next-live/", views.next_live_ai_game, name="next_live_ai_game"),
    path("<int:game_id>/spectate/", views.spectate_game, name="spectate"),
    path("<int:game_id>/", views.game_detail, name="game_detail"),
    path("<int:game_id>/comments/", views.game_comments, name="game_comments"),
    path("<int:game_id>/comments/add/", views.add_comment, name="add_comment"),
    path("history/", views.game_history, name="history"),
]
