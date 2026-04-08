from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(
        r"ws/tournaments/(?P<tournament_id>\d+)/$",
        consumers.TournamentConsumer.as_asgi(),
    ),
    re_path(
        r"ws/match/(?P<match_id>\d+)/$",
        consumers.LiveMatchConsumer.as_asgi(),
    ),
]
