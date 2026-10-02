from datetime import timedelta
from importlib import import_module

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

FROM = [("tournaments", "0030_tournament_terms_fk")]
TO = [("tournaments", "0031_seed_tournament_terms")]


class SeedTournamentTermsMigrationTest(TransactionTestCase):
    """Runs 0031 forward/backward against hand-built pre-migration rows."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("mig", password="x")
        self.old_apps = self._migrate(FROM)

    def tearDown(self):
        # Clear fixtures with the models of the schema the DB is at right now (it may be
        # mid-history), so conflicting-version rows cannot make 0031 refuse to re-run.
        executor = MigrationExecutor(connection)
        newest = max(name for app, name in executor.loader.applied_migrations if app == "tournaments")
        current = executor.loader.project_state([("tournaments", newest)]).apps
        current.get_model("tournaments", "Tournament").objects.all().delete()
        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    @staticmethod
    def _migrate(targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        executor.loader.build_graph()
        return executor.loader.project_state(targets).apps

    def _tournament(self, ttype, version="1.0", **kw):
        return self.old_apps.get_model("tournaments", "Tournament").objects.create(
            name=f"{ttype}-{version}", type=ttype, terms_version=version,
            start_time=timezone.now() + timedelta(days=1), is_money_tournament=True, **kw,
        )

    def _participant(self, tournament, version="1.0"):
        return self.old_apps.get_model("tournaments", "TournamentParticipant").objects.create(
            tournament=tournament, user_id=self.user.pk,
            terms_accepted_at=timezone.now(), terms_version_accepted=version,
        )

    def _fresh(self, apps, model, pk):
        return apps.get_model("tournaments", model).objects.get(pk=pk)

    def test_gauntlet_like_linked_and_qa_unlinked(self):
        qa = self._tournament("qa")
        gauntlet = self._tournament("gauntlet")
        mania = self._tournament("gladiatormania")
        p_gauntlet = self._participant(gauntlet)
        p_qa = self._participant(qa)

        apps = self._migrate(TO)
        self.assertIsNone(self._fresh(apps, "Tournament", qa.pk).terms)
        g = self._fresh(apps, "Tournament", gauntlet.pk).terms
        m = self._fresh(apps, "Tournament", mania.pk).terms
        self.assertEqual((g.slug, g.version, g.title), ("gladiator-gauntlet", "1.0", "Gladiator Gauntlet"))
        self.assertEqual((m.slug, m.version, m.title), ("gladiatormania", "1.0", "The Gladiatormania"))
        for rec in (g, m):
            self.assertTrue(rec.requires_age_18 and rec.requires_israeli_residency and rec.has_prize)
            self.assertIn("{{ tournament.prize_amount }}", rec.body)
            self.assertIn("{{ tournament.prize_currency }}", rec.body)
        self.assertIn("The Gladiator Gauntlet", g.body)
        self.assertNotIn("Gauntlet", m.body)
        self.assertEqual(self._fresh(apps, "TournamentParticipant", p_gauntlet.pk).accepted_terms_slug, "gladiator-gauntlet")
        self.assertEqual(self._fresh(apps, "TournamentParticipant", p_qa.pk).accepted_terms_slug, "")

    def test_gauntlet_version_matches_existing_version(self):
        t = self._tournament("gauntlet", version="2.5")
        p = self._participant(t, version="2.5")
        apps = self._migrate(TO)
        record = self._fresh(apps, "Tournament", t.pk).terms
        self.assertEqual(record.version, "2.5")
        self.assertEqual(self._fresh(apps, "TournamentParticipant", p.pk).accepted_terms_slug, record.slug)

    def test_conflicting_legacy_versions_abort(self):
        self._tournament("gauntlet", version="1.0")
        self._tournament("gauntlet", version="2.0")
        with self.assertRaises(RuntimeError):
            self._migrate(TO)

    def test_seed_is_idempotent(self):
        t = self._tournament("gauntlet")
        apps = self._migrate(TO)
        module = import_module("apps.tournaments.migrations.0031_seed_tournament_terms")
        module.seed_terms(apps, None)
        self.assertEqual(apps.get_model("tournaments", "TournamentTerms").objects.count(), 2)
        self.assertEqual(self._fresh(apps, "Tournament", t.pk).terms.slug, "gladiator-gauntlet")

    def test_reverse_unlinks_and_deletes_unreferenced_records(self):
        t = self._tournament("gauntlet")
        apps = self._migrate(TO)
        self.assertIsNotNone(self._fresh(apps, "Tournament", t.pk).terms_id)
        apps = self._migrate(FROM)
        self.assertIsNone(self._fresh(apps, "Tournament", t.pk).terms_id)
        self.assertEqual(apps.get_model("tournaments", "TournamentTerms").objects.count(), 0)

    def test_reverse_keeps_records_still_referenced(self):
        qa = self._tournament("qa")
        gauntlet = self._tournament("gauntlet")
        apps = self._migrate(TO)
        record = self._fresh(apps, "Tournament", gauntlet.pk).terms
        Tournament = apps.get_model("tournaments", "Tournament")
        Tournament.objects.filter(pk=qa.pk).update(terms=record)  # bypasses clean()

        apps = self._migrate(FROM)
        self.assertIsNone(self._fresh(apps, "Tournament", gauntlet.pk).terms_id)
        self.assertEqual(self._fresh(apps, "Tournament", qa.pk).terms_id, record.pk)
        self.assertEqual(apps.get_model("tournaments", "TournamentTerms").objects.count(), 1)
