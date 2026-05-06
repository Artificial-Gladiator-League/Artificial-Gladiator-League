# ──────────────────────────────────────────────
# agladiator/settings.py — Artificial Gladiator
# ──────────────────────────────────────────────
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-CHANGE-ME-in-production-!@#$%^&*()",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# ── Applications ──────────────────────────────
INSTALLED_APPS = [
    "daphne",                       # ASGI server (must be before django apps)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project apps
    "apps.core",
    "apps.users",
    "apps.tournaments",
    "apps.games",
    # Forum and chat apps removed
    # Third‑party
    "channels",
]

# ── Middleware ─────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.users.middleware.ModelIntegrityMiddleware",
    "apps.tournaments.middleware.DisqualificationInterceptMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# WhiteNoise — production static-file serving (pip install whitenoise)
try:
    import whitenoise  # noqa: F401
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
except ImportError:
    pass

ROOT_URLCONF = "agladiator.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "agladiator.wsgi.application"
ASGI_APPLICATION = "agladiator.asgi.application"

# ── Database (MySQL) ──────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("DB_NAME", "aigladiator"),
        "USER": os.environ.get("DB_USER", "root"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "yuval2014"),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

# ── Channel Layers (Redis) ────────────────────
# Use Redis in production; fall back to in-memory for local dev if Redis is unavailable.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

# ── Cache (Redis) ─────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# ── User model storage ────────────────────────
# Per-user cached model storage (populated on verification, cleaned on logout).
# Default is a system path on Linux; on Windows, fall back to a project-local
# directory so paths resolve correctly during development.
_DEFAULT_USER_MODELS = "/var/lib/agladiator/user_models"
_DEFAULT_SHARED_MODELS = "/var/lib/agladiator/shared_models"
if sys.platform == "win32":
    _DEFAULT_USER_MODELS = str(BASE_DIR / "user_models")
    _DEFAULT_SHARED_MODELS = str(BASE_DIR / "shared_models")
USER_MODELS_BASE_DIR = Path(os.environ.get("AGL_USER_MODELS_DIR", _DEFAULT_USER_MODELS))
SHARED_MODELS_BASE_DIR = Path(os.environ.get("AGL_SHARED_MODELS_DIR", _DEFAULT_SHARED_MODELS))

# Live session models root — per-user live folders used while the user is logged in.
# These folders are created at login and removed at logout. Override with
# the LIVE_MODELS_ROOT environment variable if desired.
_DEFAULT_LIVE_ROOT = str(USER_MODELS_BASE_DIR / "live")
LIVE_MODELS_ROOT = Path(os.environ.get("LIVE_MODELS_ROOT", _DEFAULT_LIVE_ROOT))

# Persistent model cache root used by download/verify routines. If
# `MODEL_CACHE_ROOT` is set in the environment it will be used; otherwise
# fall back to `USER_MODELS_BASE_DIR` for compatibility.
MODEL_CACHE_ROOT = Path(os.environ.get("MODEL_CACHE_ROOT", str(USER_MODELS_BASE_DIR)))

# Ensure Hugging Face cache environment variables point into the model cache
# so HF libraries (huggingface_hub, transformers) use the same local cache
# directory. These can be overridden in the environment if needed.
HF_HOME = os.environ.get("HF_HOME", str(MODEL_CACHE_ROOT / "hf_home"))
HF_HUB_CACHE = os.environ.get("HF_HUB_CACHE", str(MODEL_CACHE_ROOT / "hf_hub_cache"))
os.environ.setdefault("HF_HOME", HF_HOME)
os.environ.setdefault("HF_HUB_CACHE", HF_HUB_CACHE)

# Cache freshness TTL (days) used when deciding whether to re-download
# models. Set via env var `USER_MODELS_CACHE_DAYS`.
USER_MODELS_CACHE_DAYS = int(os.environ.get("USER_MODELS_CACHE_DAYS", "7"))

# Allow per-move downloads? Default False to avoid network traffic during moves.
ALLOW_PER_MOVE_DOWNLOADS = os.environ.get("ALLOW_PER_MOVE_DOWNLOADS", "False").lower() in ("true", "1", "yes")

# Python binary used for local (non-Docker) sandbox fallback.
SANDBOX_PYTHON_BIN = os.environ.get("SANDBOX_PYTHON_BIN", sys.executable)
# Enable safe local process fallback when Docker is unavailable (default True).
SANDBOX_ENABLE_LOCAL_FALLBACK = os.environ.get("SANDBOX_ENABLE_LOCAL_FALLBACK", "True").lower() in ("true", "1", "yes")

CHANNEL_LAYERS = {
    "default": {
        # NOTE: must be the pubsub layer, NOT channels_redis.core.RedisChannelLayer.
        # The .core layer issues BZPOPMIN, which only exists in Redis ≥ 5.0.
        # The MicrosoftArchive Redis 3.0.504 build (the easy Windows install)
        # does not support BZPOPMIN and would crash every WebSocket worker
        # with: redis.exceptions.ResponseError: unknown command 'BZPOPMIN'.
        # The pubsub layer uses PUBLISH/SUBSCRIBE only and works on any Redis.
        "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    },
}

# Auto-detect Redis availability in DEBUG mode and fall back to in-memory
if DEBUG:
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
        _r.ping()
    except Exception:
        CHANNEL_LAYERS = {
            "default": {
                "BACKEND": "channels.layers.InMemoryChannelLayer",
            },
        }
        CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        }
        CELERY_TASK_ALWAYS_EAGER = True
        CELERY_TASK_EAGER_PROPAGATES = True
        CELERY_BROKER_URL = "memory://"
        CELERY_RESULT_BACKEND = "cache+memory://"

# ── Auth ───────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/users/login/"
LOGIN_REDIRECT_URL = "/games/lobby/"
LOGOUT_REDIRECT_URL = "/"

AUTH_USER_MODEL = "users.CustomUser"

# ── Email ─────────────────────────────────────
# Console backend for development; override with SMTP in production.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL",
    "noreply@artificialgladiator.com",
)

# Countries blocked from registration (ISO 3166‑1 alpha‑2)
PROHIBITED_COUNTRIES = ["KP", "IR", "SY", "CU", "SD"]

# ── Internationalisation ──────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── Static files ──────────────────────────────
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_AUTOREFRESH = True

# ── Media / AI uploads ────────────────────────
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Allowed upload extensions for AI bots
AI_UPLOAD_ALLOWED_EXTENSIONS = [".py", ".pth"]
AI_UPLOAD_MAX_SIZE_MB = 50

# ── Hugging Face webhook ──────────────────────
# Shared secret used to verify HMAC-SHA256 signatures on incoming
# HF webhook payloads.  Generate a strong random value for production:
#   python -c "import secrets; print(secrets.token_hex(32))"
HF_WEBHOOK_SECRET = os.environ.get("HF_WEBHOOK_SECRET", "")

# ── Hugging Face Inference Endpoints ──────────
# Platform-level HF token used to download models from private
# repos for verification.  Must have scope: read-repos
# Generate at https://huggingface.co/settings/tokens
HF_PLATFORM_TOKEN = os.environ.get("HF_PLATFORM_TOKEN", "")

# ── Docker sandbox settings ───────────────────
# Base Docker image used for sandboxed inference.
SANDBOX_DOCKER_IMAGE = os.environ.get(
    "SANDBOX_DOCKER_IMAGE", "python:3.11-slim"
)

# Timeout (seconds) for a single sandbox move request.
SANDBOX_MOVE_TIMEOUT = int(os.environ.get("SANDBOX_MOVE_TIMEOUT", "30"))

# Timeout (seconds) for the full verification pipeline
# (download + scan + sandbox test positions).
SANDBOX_VERIFY_TIMEOUT = int(os.environ.get("SANDBOX_VERIFY_TIMEOUT", "300"))

# ── Hugging Face OAuth / OpenID Connect ───────
# Register your app at https://huggingface.co/settings/connected-applications
# Set the redirect URI to:  https://<your-domain>/users/oauth/hf/callback/
HF_OAUTH_CLIENT_ID = os.environ.get("HF_OAUTH_CLIENT_ID", "")
HF_OAUTH_CLIENT_SECRET = os.environ.get("HF_OAUTH_CLIENT_SECRET", "")
HF_OAUTH_SCOPES = "openid profile read-repos"
HF_OAUTH_AUTHORIZE_URL = "https://huggingface.co/oauth/authorize"
HF_OAUTH_TOKEN_URL = "https://huggingface.co/oauth/token"
HF_OAUTH_USERINFO_URL = "https://huggingface.co/oauth/userinfo"

# ── AI model preloading ───────────────────────────────
# Downloaded once at startup into HF_HUB_CACHE; no per-user copies.
# Override via AGL_CHESS_PRELOAD / AGL_BREAKTHROUGH_PRELOAD env vars
# (comma-separated repo IDs, e.g. "test1978/chess-model,other/repo").
_chess_env = os.environ.get("AGL_CHESS_PRELOAD", "test1978/chess-model")
_bt_env = os.environ.get("AGL_BREAKTHROUGH_PRELOAD", "test1978/breakthrough-model")
CHESS_PRELOAD_REPOS: list[str] = [r.strip() for r in _chess_env.split(",") if r.strip()]
BREAKTHROUGH_PRELOAD_REPOS: list[str] = [r.strip() for r in _bt_env.split(",") if r.strip()]

# ── WhiteNoise (static files in production) ───
try:
    import whitenoise  # noqa: F401
    STORAGES = {
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
except ImportError:
    pass

# ── Security notes ─────────────────────────────
# AI files MUST be executed inside a sandboxed environment
# (e.g. Docker containers with no network, limited CPU/RAM).
# Never run uploaded Python directly in the main process.

# ── Production security hardening ──────────────
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True").lower() in ("true", "1")

# ── Logging ────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "colored": {
            "format": "\n\033[96m[%(asctime)s]\033[0m \033[1m%(levelname)s\033[0m \033[93m%(name)s\033[0m → %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "colored",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.games": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.users": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.games.predict_chess": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps.games.predict_breakthrough": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps.games.bot_runner": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps.games.consumers": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Celery configuration ──────────────────────
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"

# Celery Beat — periodic task schedule
# Run with:  celery -A agladiator beat -l info
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "schedule-tournaments-weekly": {
        "task": "apps.tournaments.tasks.schedule_weekly_tournaments",
        "schedule": 604_800.0,  # every 7 days (seconds)
        # For crontab use:
        # "schedule": crontab(day_of_week="monday", hour=0, minute=0),
    },
    "check-stale-tournaments": {
        "task": "apps.tournaments.tasks.check_stale_tournaments",
        "schedule": 300.0,  # every 5 minutes
    },
    "gladiator-gauntlet-weekly": {
        "task": "apps.tournaments.tasks.run_gladiator_gauntlet",
        "schedule": 604_800.0,  # every 7 days
        # For precise Sunday 20:00 UTC use:
        # "schedule": crontab(day_of_week="sunday", hour=20, minute=0),
        "kwargs": {"participants": 16, "rounds": 5, "time_control": "3+1"},
    },
    # Daily integrity check REMOVED — integrity checks now run only at:
    #   • tournament registration (integrity.py: can_join_tournament)
    #   • before each round (engine.py: pre_round_sha_check)
    #   • probabilistically during rounds (tasks.py: run_probabilistic_sha_audit)
    "probabilistic-sha-audit": {
        "task": "apps.tournaments.tasks.run_probabilistic_sha_audit",
        "schedule": 30.0,  # every 30 seconds
    },

    "cleanup-orphaned-dirs": {
        "task": "apps.users.model_lifecycle.cleanup_orphaned_dirs",
        "schedule": 1800.0,
    },
}