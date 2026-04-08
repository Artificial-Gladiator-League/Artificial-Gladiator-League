# ──────────────────────────────────────────────
# mysite/wsgi.py
# ──────────────────────────────────────────────
import os

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agladiator.settings")
application = get_wsgi_application()
