from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

from apps.core.routing import websocket_urlpatterns as core_ws
from apps.games.routing import websocket_urlpatterns as games_ws
from apps.tournaments.routing import websocket_urlpatterns as tournaments_ws

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(
                URLRouter(
                    games_ws + tournaments_ws + core_ws
                )
            )
        ),
    }
)
