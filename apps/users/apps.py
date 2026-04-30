from django.apps import AppConfig
import threading
import logging
import os


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "Users & Auth"

    def ready(self):
        import apps.users.signals  # noqa: F401 — register post_save handlers

        # Startup pre-warm: ensure bot models (model_integrity_ok=True)
        # that have no `cached_path` are downloaded into the persistent
        # cache so they survive server restarts. Run in a background
        # thread to avoid blocking process startup.
        def _startup_warm():
            log = logging.getLogger(__name__)
            # Respect PREWARM_MODELS environment toggle (set to 'false' to skip)
            try:
                if os.environ.get("PREWARM_MODELS") == "false":
                    log.info("Pre-warm skipped")
                    return
            except Exception:
                # If environment access fails for any reason, proceed with warm
                pass
            try:
                from django.db.models import Q
                from apps.users.models import UserGameModel
                from apps.users.model_lifecycle import download_and_scan_for_user

                q = UserGameModel.objects.filter(model_integrity_ok=True).filter(
                    Q(cached_path__isnull=True) | Q(cached_path=""),
                )
                user_ids = sorted({gm.user_id for gm in q})
                if not user_ids:
                    return
                log.info("Startup pre-warm: warming cached models for %d users", len(user_ids))
                for uid in user_ids:
                    try:
                        download_and_scan_for_user(uid)
                    except Exception:
                        log.exception("Startup warm failed for user %s", uid)
            except Exception:
                log = logging.getLogger(__name__)
                log.exception("Startup pre-warm failed")

        try:
            t = threading.Thread(target=_startup_warm, daemon=True)
            t.start()
        except Exception:
            logging.getLogger(__name__).exception("Failed to start startup pre-warm thread")
