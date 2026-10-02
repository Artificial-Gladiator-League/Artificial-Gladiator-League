from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from apps.tournaments.admin import TournamentAdmin
from apps.tournaments.countries import default_allowed_countries, normalize_country_codes, unknown_country_codes
from apps.tournaments.forms import CountryCodesField
from apps.tournaments.models import Tournament, TournamentTerms


def _terms(slug="ac-terms", **kw):
    defaults = dict(
        title="AC", version="1.0", body="<p>x</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(slug=slug, **defaults)


def _tournament(ttype=Tournament.Type.GLADIATORMANIA, **kw):
    defaults = dict(
        name="AC", type=ttype, start_time=timezone.now() + timedelta(days=1),
        is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="INR",
    )
    defaults.update(kw)
    return Tournament(**defaults)


class CountryHelpersTest(TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_country_codes(" in, il ;in  us"), ["IN", "IL", "US"])
        self.assertEqual(normalize_country_codes(["in", "IN", " il "]), ["IN", "IL"])
        self.assertEqual(normalize_country_codes(None), [])
        self.assertEqual(normalize_country_codes({"a": 1}), [])

    def test_unknown_codes(self):
        self.assertEqual(unknown_country_codes(["IN", "ZZ", "USA", "I"]), ["ZZ", "USA", "I"])

    def test_default_is_a_fresh_israel_only_list(self):
        first, second = default_allowed_countries(), default_allowed_countries()
        self.assertEqual(first, ["IL"])
        first.append("IN")
        self.assertEqual(second, ["IL"])


class AllowedCountriesFieldTest(TestCase):
    def test_new_tournaments_default_to_israel_only(self):
        t = Tournament.objects.create(
            name="Default", type=Tournament.Type.GAUNTLET, start_time=timezone.now() + timedelta(days=1),
        )
        t.refresh_from_db()
        self.assertEqual(t.allowed_countries, ["IL"])
        self.assertTrue(t.is_israel_only)

    def test_clean_normalises_codes(self):
        t = _tournament(allowed_countries=["in", "IN"], terms=None, prize_amount=None, is_money_tournament=False)
        t.clean()
        self.assertEqual(t.allowed_countries, ["IN"])
        self.assertFalse(t.is_israel_only)

    def test_clean_rejects_empty_and_unknown_codes(self):
        for value, text in (([], "at least one"), (["ZZ"], "Unknown"), ("", "at least one")):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    _tournament(allowed_countries=value, terms=None, prize_amount=None).clean()
                self.assertIn(text, " ".join(ctx.exception.message_dict["allowed_countries"]))

    def test_only_gladiatormania_can_leave_israel(self):
        for ttype in (Tournament.Type.GAUNTLET, Tournament.Type.QA):
            with self.subTest(type=ttype):
                with self.assertRaises(ValidationError) as ctx:
                    _tournament(ttype, allowed_countries=["IN"], prize_amount=None).clean()
                self.assertIn("stay Israel-only", " ".join(ctx.exception.message_dict["allowed_countries"]))
        _tournament(allowed_countries=["IN"], prize_amount=None, is_money_tournament=False).clean()
        _tournament(allowed_countries=["IN", "IL"], prize_amount=None, is_money_tournament=False).clean()

    def test_gauntlet_israel_only_is_still_valid(self):
        _tournament(Tournament.Type.GAUNTLET, allowed_countries=["IL"], prize_amount=None).clean()


class ResidencyRuleWithCountriesTest(TestCase):
    def _errors(self, tournament):
        with self.assertRaises(ValidationError) as ctx:
            tournament.clean()
        return " | ".join(ctx.exception.message_dict["terms"])

    def test_israel_only_keeps_the_original_message(self):
        terms = _terms(requires_israeli_residency=False)
        t = _tournament(Tournament.Type.GAUNTLET, allowed_countries=["IL"], terms=terms)
        message = self._errors(t)
        self.assertIn("Not supported yet", message)
        self.assertIn("geo check and eligibility verification still require Israeli residency", message)

    def test_other_countries_still_need_a_residency_declaration(self):
        terms = _terms(slug="ac-in", requires_israeli_residency=False)
        t = _tournament(allowed_countries=["IN"], terms=terms)
        message = self._errors(t)
        self.assertIn("restricted to IN", message)
        self.assertIn("must require a residency declaration", message)

    def test_valid_combinations(self):
        _tournament(allowed_countries=["IN"], terms=_terms(slug="ac-ok")).clean()
        _tournament(Tournament.Type.GAUNTLET, terms=_terms(slug="ac-gauntlet")).clean()

    def test_errors_for_countries_and_terms_are_reported_together(self):
        terms = _terms(slug="ac-both", requires_israeli_residency=False)
        t = _tournament(Tournament.Type.GAUNTLET, allowed_countries=["IN"], terms=terms)
        with self.assertRaises(ValidationError) as ctx:
            t.clean()
        self.assertEqual(set(ctx.exception.message_dict), {"allowed_countries", "terms"})


class CountryCodesAdminFieldTest(TestCase):
    def test_form_field_parses_and_validates(self):
        field = CountryCodesField()
        self.assertEqual(field.clean("in, il"), ["IN", "IL"])
        self.assertEqual(field.clean(""), [])
        with self.assertRaises(ValidationError):
            field.clean("IN, ZZ")
        self.assertEqual(field.prepare_value(["IN", "IL"]), "IN, IL")
        self.assertFalse(field.has_changed(["IL"], "il"))
        self.assertTrue(field.has_changed(["IL"], "IN"))

    def _form(self, **extra):
        from django.contrib.auth import get_user_model
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.get_or_create(
            username="adm", defaults=dict(is_staff=True, is_superuser=True),
        )[0]
        form_class = TournamentAdmin(Tournament, admin.site).get_form(request)
        start = timezone.now() + timedelta(days=2)
        data = {
            "name": "Form", "type": "gladiatormania", "game_type": "chess", "time_control": "3+1",
            "capacity": 2, "rounds_total": 1, "current_round": 0, "status": "open",
            "start_time_0": start.strftime("%Y-%m-%d"), "start_time_1": start.strftime("%H:%M:%S"),
            "is_money_tournament": "on", "prize_amount": "500.00", "prize_currency": "INR",
            "payout_status": "pending", "payout_method": "paypal", "claim_deadline_days": 30,
        }
        data.update(extra)
        return form_class(data)

    def test_admin_saves_a_list_and_defaults_when_omitted(self):
        terms = _terms(slug="ac-admin")
        form = self._form(allowed_countries="in", terms=terms.pk)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.allowed_countries, ["IN"])

        omitted = self._form()
        self.assertTrue(omitted.is_valid(), omitted.errors)
        self.assertEqual(omitted.instance.allowed_countries, ["IL"])

    def test_admin_rejects_blank_unknown_and_gauntlet_outside_israel(self):
        for extra, text in (
            ({"allowed_countries": ""}, "at least one"),
            ({"allowed_countries": "ZZ"}, "Unknown"),
            ({"allowed_countries": "IN", "type": "gauntlet"}, "stay Israel-only"),
        ):
            with self.subTest(extra=extra):
                form = self._form(**extra)
                self.assertFalse(form.is_valid())
                self.assertIn(text, " ".join(form.errors["allowed_countries"]))

    def test_admin_shows_the_current_value(self):
        t = Tournament.objects.create(
            name="Shown", type=Tournament.Type.GLADIATORMANIA, allowed_countries=["IN"],
            start_time=timezone.now() + timedelta(days=1),
        )
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_superuser("adm2", "b@x.com", "pw"))
        resp = self.client.get(f"/admin/tournaments/tournament/{t.pk}/change/")
        self.assertContains(resp, 'name="allowed_countries"')
        self.assertContains(resp, 'value="IN"')


class AllowedCountriesMigrationTest(TransactionTestCase):
    FROM = [("tournaments", "0031_seed_tournament_terms")]
    TO = [("tournaments", "0032_tournament_allowed_countries")]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        executor.loader.build_graph()
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        Tournament.objects.all().delete()

    def test_existing_tournaments_become_israel_only(self):
        old_apps = self._migrate(self.FROM)
        Old = old_apps.get_model("tournaments", "Tournament")
        ids = [
            Old.objects.create(
                name=f"{ttype}", type=ttype, start_time=timezone.now() + timedelta(days=1),
            ).pk
            for ttype in ("gauntlet", "gladiatormania", "qa")
        ]
        new_apps = self._migrate(self.TO)
        New = new_apps.get_model("tournaments", "Tournament")
        self.assertEqual([New.objects.get(pk=pk).allowed_countries for pk in ids], [["IL"]] * 3)

        back = self._migrate(self.FROM)
        self.assertFalse(any(f.name == "allowed_countries" for f in back.get_model("tournaments", "Tournament")._meta.fields))
