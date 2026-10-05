from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentTerms


def _terms(slug="clean-terms", **kw):
    defaults = dict(
        slug=slug, title="Clean", version="1.0", body="<p>x</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(**defaults)


def _tournament(terms, **kw):
    defaults = dict(
        name="C", type=Tournament.Type.GAUNTLET, start_time=timezone.now() + timedelta(days=1),
        is_money_tournament=True, prize_amount=Decimal("500.00"), terms=terms,
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


class TournamentCleanTermsRulesTest(TestCase):
    def _errors(self, tournament):
        with self.assertRaises(ValidationError) as ctx:
            tournament.clean()
        return " | ".join(ctx.exception.message_dict["terms"])

    def test_consistent_configuration_is_valid(self):
        for ttype in Tournament.MONEY_LIKE_TYPES:
            _tournament(_terms(slug=f"ok-{ttype}"), type=ttype).clean()

    def test_no_terms_is_always_valid(self):
        _tournament(None, prize_amount=None, is_money_tournament=False).clean()

    def test_residency_flag_off_is_not_supported_yet(self):
        t = _tournament(_terms(requires_israeli_residency=False))
        message = self._errors(t)
        self.assertIn("Not supported yet", message)
        self.assertIn("geo check and eligibility verification still require Israeli residency", message)

    def test_terms_without_prize_on_a_tournament_with_a_prize(self):
        t = _tournament(_terms(has_prize=False))
        self.assertIn("no prize", self._errors(t))

    def test_terms_with_prize_on_a_tournament_without_prize_amount(self):
        terms = _terms(slug="prize-terms")
        for amount in (None, Decimal("0")):
            with self.subTest(amount=amount):
                t = _tournament(terms, prize_amount=amount)
                self.assertIn("no prize amount", self._errors(t))

    def test_prize_free_terms_on_a_prize_free_tournament_are_valid(self):
        _tournament(_terms(slug="free", has_prize=False), prize_amount=None).clean()

    def test_all_problems_are_reported_together(self):
        t = _tournament(_terms(slug="bad", has_prize=False, requires_israeli_residency=False))
        message = self._errors(t)
        self.assertIn("Not supported yet", message)
        self.assertIn("no prize", message)
