"""Guest mode foundation: model fields, flag guard, system check, reversible migration."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.checks import run_checks
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.conf import settings

from apps.games.models import Game
from apps.users.models import UserGameModel

User = get_user_model()


class GuestDefaultsTests(SimpleTestCase):
    def test_guest_mode_is_off_by_default(self):
        self.assertFalse(settings.GUEST_MODE_ENABLED)
        self.assertEqual(settings.GUEST_TTL_DAYS, 3)
        self.assertEqual(settings.GUEST_SNAPSHOT_IDLE_MINUTES, 120)
        self.assertEqual(settings.GUEST_CELERY_QUEUE, "celery")
        self.assertEqual(settings.GUEST_OAUTH_SCOPES, "openid profile")
        self.assertEqual(settings.SANDBOX_DOCKER_RUNTIME, "")
        self.assertEqual(settings.TRUSTED_PROXY_HEADER, "")


class GuestModelFieldTests(TestCase):
    def test_user_defaults(self):
        u = User.objects.create_user("regular", password="pw")
        self.assertFalse(u.is_guest)
        self.assertIsNone(u.hf_sub)
        self.assertEqual(u.guest_hf_username, "")
        self.assertEqual(u.hf_username, "")

    def test_hf_sub_is_unique_but_nullable(self):
        User.objects.create_user("a", password="pw")
        User.objects.create_user("b", password="pw")
        User.objects.create_user("c", password="pw", hf_sub="sub-1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("d", password="pw", hf_sub="sub-1")

    def test_verification_method_defaults_to_file(self):
        u = User.objects.create_user("m", password="pw")
        gm = UserGameModel.objects.create(user=u, game_type="chess", hf_model_repo_id="m/model")
        self.assertEqual(gm.verification_method, "file")


class GuestGameFlagTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("ga", password="pw")
        self.b = User.objects.create_user("gb", password="pw")

    def test_flag_cannot_be_cleared_on_loaded_instance(self):
        game = Game.objects.create(white=self.a, black=self.b, is_guest_game=True)
        loaded = Game.objects.get(pk=game.pk)
        loaded.is_guest_game = False
        loaded.save()
        self.assertTrue(Game.objects.get(pk=game.pk).is_guest_game)

    def test_flag_cannot_be_cleared_with_update_fields(self):
        game = Game.objects.create(white=self.a, black=self.b, is_guest_game=True)
        loaded = Game.objects.get(pk=game.pk)
        loaded.is_guest_game = False
        loaded.save(update_fields=["is_guest_game"])
        self.assertTrue(Game.objects.get(pk=game.pk).is_guest_game)

    def test_flag_cannot_be_cleared_on_the_creating_instance(self):
        game = Game.objects.create(white=self.a, black=self.b, is_guest_game=True)
        game.is_guest_game = False
        game.save()
        self.assertTrue(Game.objects.get(pk=game.pk).is_guest_game)

    def test_regular_game_stays_false(self):
        game = Game.objects.create(white=self.a, black=self.b)
        self.assertFalse(Game.objects.get(pk=game.pk).is_guest_game)


class GuestSystemCheckTests(SimpleTestCase):
    def _ids(self):
        return {m.id for m in run_checks()}

    def test_local_fallback_with_guest_mode_is_an_error(self):
        with override_settings(GUEST_MODE_ENABLED=True, SANDBOX_ENABLE_LOCAL_FALLBACK=True):
            self.assertIn("users.E001", self._ids())

    def test_guest_mode_with_docker_only_is_clean(self):
        with override_settings(GUEST_MODE_ENABLED=True, SANDBOX_ENABLE_LOCAL_FALLBACK=False,
                               HF_OAUTH_CLIENT_ID="x"):
            ids = self._ids()
        self.assertNotIn("users.E001", ids)
        self.assertNotIn("users.W001", ids)

    def test_checks_are_silent_when_disabled(self):
        with override_settings(GUEST_MODE_ENABLED=False, SANDBOX_ENABLE_LOCAL_FALLBACK=True):
            ids = self._ids()
        self.assertFalse({i for i in ids if i.startswith("users.")})


class GuestMigrationReversibleTests(TransactionTestCase):
    def _columns(self, table):
        with connection.cursor() as cur:
            return {c.name for c in connection.introspection.get_table_description(cur, table)}

    def test_guest_migrations_can_be_reversed_and_reapplied(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([
                ("users", "0041_diagnosticrun"),
                ("games", "0011_alter_game_ai_thinking_seconds_and_more"),
            ])
            self.assertNotIn("is_guest", self._columns("users_user"))
            self.assertNotIn("verification_method", self._columns("users_usergamemodel"))
            self.assertNotIn("is_guest_game", self._columns("games_game"))
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        self.assertIn("is_guest", self._columns("users_user"))
        self.assertIn("hf_sub", self._columns("users_user"))
        self.assertIn("verification_method", self._columns("users_usergamemodel"))
        self.assertIn("is_guest_game", self._columns("games_game"))
