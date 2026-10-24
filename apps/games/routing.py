from django.urls import re_path
from . import consumers
from .shiri_consumer import ShiRiConsumer

websocket_urlpatterns = [
    re_path(r"ws/games/lobby/$", consumers.LobbyConsumer.as_asgi()),
    re_path(r"ws/shiri/$", ShiRiConsumer.as_asgi()),
    re_path(r"ws/games/(?P<game_id>\d+)/spectate/$", consumers.SpectateConsumer.as_asgi()),
    re_path(r"ws/games/(?P<game_id>\d+)/$", consumers.GameConsumer.as_asgi()),
]
