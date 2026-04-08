import logging

from django.apps import AppConfig

log = logging.getLogger(__name__)


class GamesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.games"
    verbose_name = "Games & Quick Pairing"

    def ready(self):
        # Models are no longer loaded locally.  All AI inference is
        # handled via HF Inference Endpoints (see apps/users/hf_inference.py),
        # so there is nothing to preload at server startup.
        log.info("GamesConfig.ready() — no local model preloading (using HF Inference Endpoints).")
