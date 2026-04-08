# ──────────────────────────────────────────────
# agladiator/asgi.py — Channels + WebSocket routing
# ──────────────────────────────────────────────
import os

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agladiator.settings")

django_asgi_app = get_asgi_application()

# Import after Django setup so apps are loaded
from apps.core.routing import websocket_urlpatterns as core_ws                # noqa: E402
from apps.games.routing import websocket_urlpatterns as games_ws              # noqa: E402
from apps.tournaments.routing import websocket_urlpatterns as tournaments_ws  # noqa: E402
from apps.chat.routing import websocket_urlpatterns as chat_ws                # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(
                URLRouter(
                    games_ws + tournaments_ws + core_ws + chat_ws
                )
            )
        ),
    }
)