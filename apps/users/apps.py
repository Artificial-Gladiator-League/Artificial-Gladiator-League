from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "Users & Auth"

    def ready(self):
        import apps.users.signals  # noqa: F401 — register post_save handlers

        # Start the APScheduler daily integrity check
        from apps.users.scheduler import start_scheduler
        start_scheduler()
