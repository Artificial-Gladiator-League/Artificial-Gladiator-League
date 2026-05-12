# ──────────────────────────────────────────────
# agladiator/asgi.py — ASGI entry point
# ──────────────────────────────────────────────
import os

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agladiator.settings")

# Trigger Django app registry before importing routing
from django.core.asgi import get_asgi_application  # noqa: E402
get_asgi_application()

# Import the fully assembled ASGI application from routing.py
from agladiator.routing import application  # noqa: E402, F401