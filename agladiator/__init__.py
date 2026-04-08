# agladiator/__init__.py

# Ensure the Celery app is loaded when Django starts,
# so that @shared_task decorators use it.
try:
    from .celery import app as celery_app  # noqa: F401

    __all__ = ("celery_app",)
except ImportError:
    # Celery not installed — tasks degrade to plain functions.
    pass
