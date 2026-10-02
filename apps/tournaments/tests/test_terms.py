from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.terms import render_body

User = get_user_model()


def _terms(**kw):
    defaults = dict(
        slug="test-terms", title="Test Terms", version="1.0",
        body="<p>Prize {{ tournament.prize_amount }} {{ tournament.prize_currency }}</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(**defaults)


def _tournament(**kw):
    defaults = dict(
        name="T", type=Tournament.Type.GAUNTLET, start_time=timezone.now() + timedelta(days=1),
        is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="ILS",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


class TournamentTermsModelTest(TestCase):
    def test_str_and_defaults(self):
        t = _terms()
        self.assertEqual(str(t), "Test Terms v1.0")
        self.assertTrue(t.is_active)

    def test_slug_version_unique(self):
        _terms()
        with self.assertRaises(IntegrityError), transaction.atomic():
            _terms()
        _terms(version="1.1")

    def test_clean_rejects_bad_syntax_and_load(self):
        with self.assertRaises(ValidationError):
            _terms(version="2", body="{% if x %}unclosed").clean()
        with self.assertRaises(ValidationError):
            _terms(version="3", body="{% load static %}").clean()

    def test_render_uses_only_tournament_and_terms_and_escapes(self):
        terms = _terms()
        t = _tournament(name="<b>x</b>")
        self.assertEqual(render_body(terms, t), "<p>Prize 500.00 ILS</p>")
        terms.body = "{{ tournament.name }} {{ request }}|{{ terms.title }}"
        self.assertEqual(
            render_body(terms, t), "&lt;b&gt;x&lt;/b&gt; |Test Terms",
        )

    def test_acceptance_count_matches_slug_and_version(self):
        terms = _terms()
        t = _tournament()
        users = [User.objects.create_user(f"u{i}", password="x") for i in range(3)]
        TournamentParticipant.objects.create(
            tournament=t, user=users[0],
            accepted_terms_slug="test-terms", terms_version_accepted="1.0",
        )
        TournamentParticipant.objects.create(
            tournament=t, user=users[1],
            accepted_terms_slug="test-terms", terms_version_accepted="1.1",
        )
        TournamentParticipant.objects.create(
            tournament=t, user=users[2],
            accepted_terms_slug="other", terms_version_accepted="1.0",
        )
        self.assertEqual(terms.acceptance_count(), 1)


class TournamentTermsAdminTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "r@x.com", "pw")
        self.client.force_login(self.admin)

    def test_add_page_has_large_body_textarea(self):
        resp = self.client.get(reverse("admin:tournaments_tournamentterms_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'rows="40"')

    def test_change_view_warns_only_when_accepted(self):
        terms = _terms()
        url = reverse("admin:tournaments_tournamentterms_change", args=[terms.pk])
        resp = self.client.get(url)
        self.assertEqual(len(list(resp.context["messages"])), 0)

        user = User.objects.create_user("p", password="x")
        TournamentParticipant.objects.create(
            tournament=_tournament(), user=user,
            accepted_terms_slug=terms.slug, terms_version_accepted=terms.version,
        )
        resp = self.client.get(url)
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertEqual(len(msgs), 1)
        self.assertIn("already accepted", msgs[0])

    def test_changelist_loads(self):
        _terms()
        resp = self.client.get(reverse("admin:tournaments_tournamentterms_changelist"))
        self.assertEqual(resp.status_code, 200)


class TournamentTermsLinkTest(TestCase):
    def test_terms_is_optional(self):
        t = _tournament()
        self.assertIsNone(t.terms)

    def test_clean_allows_terms_only_on_money_like_types(self):
        terms = _terms()
        for ttype in Tournament.MONEY_LIKE_TYPES:
            _tournament(type=ttype, terms=terms).clean()
        with self.assertRaises(ValidationError):
            _tournament(type=Tournament.Type.QA, terms=terms).clean()

    def test_linked_terms_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        terms = _terms()
        _tournament(terms=terms)
        with self.assertRaises(ProtectedError):
            terms.delete()


class TournamentAdminTermsFieldTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.com", "pw"))
        self.active = _terms(version="2.0")
        self.inactive = _terms(version="1.0", is_active=False)

    def _queryset(self, resp):
        # Ignore the records seeded by migration 0031.
        qs = resp.context["adminform"].form.fields["terms"].queryset
        return set(qs.filter(slug="test-terms"))

    def test_add_form_lists_active_terms_only(self):
        resp = self.client.get(reverse("admin:tournaments_tournament_add"))
        self.assertEqual(self._queryset(resp), {self.active})

    def test_change_form_keeps_currently_linked_inactive_terms(self):
        t = _tournament(terms=self.inactive)
        resp = self.client.get(reverse("admin:tournaments_tournament_change", args=[t.pk]))
        self.assertEqual(self._queryset(resp), {self.active, self.inactive})
        other = _tournament(name="Other")
        resp = self.client.get(reverse("admin:tournaments_tournament_change", args=[other.pk]))
        self.assertEqual(self._queryset(resp), {self.active})
