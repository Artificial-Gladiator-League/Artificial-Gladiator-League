import logging

from django.apps import AppConfig

log = logging.getLogger(__name__)


class GamesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.games"
    verbose_name = "Games & Quick Pairing"

    def ready(self):
        log.info("GamesConfig ready — inference via HF API.")
