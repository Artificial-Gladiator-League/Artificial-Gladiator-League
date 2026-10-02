import re
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentTerms

FEE_WORDS = re.compile(r"fee|price|cost|credit|coin|wallet|purchase|checkout|billing|invoice|subscription", re.I)


def _money_tournament(slug, ttype, countries, **kw):
    terms = TournamentTerms.objects.create(
        slug=slug, version="1.0", title=slug, body="<p>x</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults = dict(
        name=slug, type=ttype, status=Tournament.Status.OPEN, game_type="chess",
        start_time=timezone.now() + timedelta(days=1), is_money_tournament=True,
        prize_amount=Decimal("500.00"), prize_currency="INR", allowed_countries=countries,
        terms=terms, terms_version="",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


class ResidentsOnlyLabelTest(SimpleTestCase):
    def test_labels(self):
        self.assertEqual(Tournament(allowed_countries=["IL"]).residents_only_label, "Israeli Residents Only")
        self.assertEqual(Tournament(allowed_countries=["IN"]).residents_only_label, "Residents of India only")
        self.assertEqual(
            Tournament(allowed_countries=["IN", "IL"]).residents_only_label, "Residents of India and Israel only",
        )


class CountryTextsTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("txt", password="x", email="t@x.com")
        self.user.paypal_email = "t@paypal.test"
        self.user.save(update_fields=["paypal_email"])
        self.israel = _money_tournament("tx-il", Tournament.Type.GAUNTLET, ["IL"], prize_currency="ILS")
        self.india = _money_tournament("tx-in", Tournament.Type.GLADIATORMANIA, ["IN"])

    def _detail(self, t):
        return self.client.get(f"/tournaments/{t.pk}/")

    def test_detail_banner_per_country(self):
        resp = self._detail(self.israel)
        self.assertContains(resp, "Israeli Residents Only")
        resp = self._detail(self.india)
        self.assertContains(resp, "Residents of India only")
        self.assertNotContains(resp, "Israeli Residents Only")
        self.assertContains(resp, "Entry restricted by IP geolocation. Winners must verify residency before payout.")

    def test_terms_page_notice_per_country(self):
        from apps.users.models import CustomUser
        self.client.force_login(self.user)
        CustomUser.objects.filter(pk=self.user.pk).update(country="IL")
        resp = self.client.get(f"/tournaments/{self.israel.pk}/terms/")
        self.assertContains(resp, "open to <strong>Israeli residents only</strong>.")
        CustomUser.objects.filter(pk=self.user.pk).update(country="IN")
        resp = self.client.get(f"/tournaments/{self.india.pk}/terms/")
        self.assertContains(resp, "open to <strong>residents of India only</strong>.")
        self.assertNotContains(resp, "Israeli residents only")

    def test_notice_hidden_when_terms_do_not_require_residency(self):
        terms = TournamentTerms.objects.create(
            slug="tx-off", version="1.0", title="x", body="<p>x</p>",
            requires_age_18=True, requires_israeli_residency=False, has_prize=True,
        )
        t = Tournament.objects.create(
            name="off", type=Tournament.Type.GAUNTLET, status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1), is_money_tournament=True,
            prize_amount=Decimal("5"), prize_currency="ILS", terms=terms,
        )
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(f"/tournaments/{t.pk}/terms/"), "Eligibility restriction")


class AdminTextTest(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("adm", "a@x.com", "pw"))

    def test_description_names_the_allowed_countries(self):
        resp = self.client.get("/admin/tournaments/tournament/add/")
        self.assertContains(resp, "residents of the countries in allowed_countries")
        self.assertNotContains(resp, "Israeli residents (IP-based)")

    def test_list_column_is_called_prize_not_entry(self):
        Tournament.objects.create(
            name="Plain", type=Tournament.Type.GAUNTLET, start_time=timezone.now() + timedelta(days=1),
        )
        _money_tournament("ad-in", Tournament.Type.GLADIATORMANIA, ["IN"])
        resp = self.client.get("/admin/tournaments/tournament/")
        self.assertContains(resp, "column-prize_display")
        self.assertNotContains(resp, "column-entry_display")
        self.assertContains(resp, "500.00 INR")
        self.assertNotIn("Entry", resp.content.decode().split("column-prize_display")[1][:300])


class NoFeeFieldsTest(SimpleTestCase):
    """Nothing in the tournaments data model may imply an entry fee, credits or paid access."""

    def test_no_model_field_suggests_a_fee(self):
        offenders = [
            f"{model.__name__}.{field.name}"
            for model in apps.get_app_config("tournaments").get_models()
            for field in model._meta.get_fields()
            if FEE_WORDS.search(field.name)
        ]
        self.assertEqual(offenders, [])
