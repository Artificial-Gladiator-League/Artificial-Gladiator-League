"""platform_limits context processor and the single TOURNAMENT_MIN_RATED_GAMES setting."""
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings

from apps.core.context_processors import platform_limits

PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)

CUSTOM = dict(
    SANDBOX_MOVE_TIMEOUT=45,
    SANDBOX_MEMORY_LIMIT_MB=768,
    SANDBOX_CPU_LIMIT=2.0,
    SANDBOX_PIDS_LIMIT=256,
    SANDBOX_WORKER_START_TIMEOUT=90,
    SANDBOX_WORKER_IDLE_SECONDS=600,
    GUEST_SNAPSHOT_IDLE_MINUTES=60,
    GUEST_TTL_DAYS=1,
    TOURNAMENT_MIN_RATED_GAMES=25,
)


def _limits():
    return platform_limits(RequestFactory().get("/"))


class PlatformLimitsTests(TestCase):
    def test_registered_in_template_settings(self):
        processors = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
        self.assertIn("apps.core.context_processors.platform_limits", processors)

    def test_undefined_worker_settings_use_the_sandbox_defaults(self):
        with override_settings():
            # settings.py does not define these two; the getattr defaults must match apps.games.sandbox_runner.
            for name in ("SANDBOX_WORKER_IDLE_SECONDS", "SANDBOX_WORKER_START_TIMEOUT"):
                if hasattr(settings, name):
                    delattr(settings, name)
            ctx = _limits()
        self.assertEqual(ctx["sandbox_cold_start_timeout"], 120)
        self.assertEqual(ctx["sandbox_idle_minutes"], 15)

    def test_default_settings_values(self):
        ctx = _limits()
        self.assertEqual(ctx["sandbox_move_timeout"], 30)
        self.assertEqual(ctx["sandbox_memory_mb"], 512)
        self.assertEqual(ctx["sandbox_cpu"], 1.0)
        self.assertEqual(ctx["sandbox_pids"], 128)
        self.assertEqual(ctx["guest_snapshot_idle_hours"], 2)
        self.assertEqual(ctx["guest_ttl_days"], 3)
        self.assertEqual(ctx["tournament_min_rated_games"], 30)

    @override_settings(**CUSTOM)
    def test_values_follow_settings(self):
        ctx = _limits()
        self.assertEqual(ctx["sandbox_move_timeout"], 45)
        self.assertEqual(ctx["sandbox_memory_mb"], 768)
        self.assertEqual(ctx["sandbox_cpu"], 2.0)
        self.assertEqual(ctx["sandbox_pids"], 256)
        self.assertEqual(ctx["sandbox_cold_start_timeout"], 90)
        self.assertEqual(ctx["sandbox_idle_minutes"], 10)
        self.assertEqual(ctx["guest_snapshot_idle_hours"], 1)
        self.assertEqual(ctx["guest_ttl_days"], 1)
        self.assertEqual(ctx["tournament_min_rated_games"], 25)


@PLAIN_STATIC
class DocPagesUseLimitsTests(TestCase):
    def _page(self, name):
        from django.urls import reverse
        return self.client.get(reverse(name)).content.decode()

    def test_pages_show_the_defaults(self):
        works = self._page("core:how_it_works")
        self.assertIn("30 seconds per move", works)
        self.assertIn("512 MB", works)
        self.assertIn("30 rated games", works)

    @override_settings(GUEST_MODE_ENABLED=True, **CUSTOM)
    def test_pages_show_configured_values(self):
        works = self._page("core:how_it_works")
        for text in ("45 seconds per move", "768 MB", "2 CPU", "<td>256</td>", "90 seconds", "10 minutes",
                     "25 rated games", "after 1 idle hour,", "after 1 day of inactivity"):
            self.assertIn(text, works)
        guide = self._page("core:how_to_upload")
        for text in ("25 rated games before you can join", "(45 seconds)", "x / 25"):
            self.assertIn(text, guide)


@PLAIN_STATIC
class TournamentHowItWorksLimitTests(TestCase):
    def _page(self):
        from django.urls import reverse
        return self.client.get(reverse("tournaments:how_it_works")).content.decode()

    def test_default(self):
        self.assertIn("30 Rated Games Required", self._page())

    @override_settings(TOURNAMENT_MIN_RATED_GAMES=25)
    def test_follows_the_setting(self):
        page = self._page()
        self.assertIn("25 Rated Games Required", page)
        self.assertIn("25 rated games</strong>", page)
        self.assertNotIn("30 Rated Games Required", page)


class TournamentGameCountSettingTests(TestCase):
    def test_integrity_and_engine_use_the_one_setting(self):
        from apps.tournaments import engine
        from apps.users import integrity

        expected = settings.TOURNAMENT_MIN_RATED_GAMES
        self.assertEqual(integrity.TOURNAMENT_MIN_RATED_GAMES, expected)
        self.assertEqual(integrity.REVALIDATION_GAMES_REQUIRED, expected)
        self.assertEqual(engine.REVALIDATION_GAMES_REQUIRED, expected)
