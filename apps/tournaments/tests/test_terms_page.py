import os
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.template import TemplateDoesNotExist, engines
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.models import Tournament, TournamentTerms
from apps.tournaments.terms import build_terms_context

User = get_user_model()

LEGACY_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "legacy_money_terms.html")


def _user(name="alice", paypal="alice@example.com"):
    user = User.objects.create_user(name, password="pw")
    user.paypal_email = paypal
    user.save(update_fields=["paypal_email"])
    return user


def _tournament(**kw):
    defaults = dict(
        name="Weekly Money", type=Tournament.Type.GAUNTLET, status=Tournament.Status.OPEN,
        start_time=timezone.now() + timedelta(days=1), is_money_tournament=True,
        prize_amount=Decimal("500.00"), prize_currency="ILS",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


def _terms(**kw):
    defaults = dict(
        slug="page-terms", title="Page Terms", version="2.3",
        body="<p>Prize {{ tournament.prize_amount }} {{ tournament.prize_currency }}</p>",
        requires_age_18=True, requires_israeli_residency=True, has_prize=True,
    )
    defaults.update(kw)
    return TournamentTerms.objects.create(**defaults)


def _normalize(html):
    html = html.replace("\r\n", "\n")
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)  # the Accept-button JS changed on purpose
    html = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', 'name="csrfmiddlewaretoken"', html)
    html = re.sub(r">\s+<", "><", html)
    return re.sub(r"\s+", " ", html).strip()


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False)
class TermsPageFallbackTest(TestCase):
    def test_no_terms_renders_like_the_legacy_template(self):
        user = _user()
        tournament = _tournament()
        request = RequestFactory().get("/")
        request.user = user

        with open(LEGACY_FIXTURE, encoding="utf-8") as f:
            legacy = engines["django"].from_string(f.read()).render({"tournament": tournament}, request)
        new = render_to_string(
            "tournaments/money_terms.html",
            {"tournament": tournament, **build_terms_context(tournament)}, request=request,
        )

        legacy_n, new_n = _normalize(legacy), _normalize(new)
        # The only intended difference: the scrolling terms box is now closed before the PayPal box.
        unclosed = '<hr class="border-gray-600"></div><!-- PayPal email confirmation -->'
        self.assertIn(unclosed, legacy_n)
        legacy_n = legacy_n.replace(
            unclosed, '<hr class="border-gray-600"></div></div><!-- PayPal email confirmation -->',
        )
        self.assertEqual(new_n, legacy_n)

    def test_fallback_context_keeps_every_box_and_checkbox(self):
        ctx = build_terms_context(_tournament())
        self.assertIsNone(ctx["terms"])
        self.assertEqual((ctx["terms_title"], ctx["terms_version"]), ("Money Tournament", "1.0"))
        for flag in ("show_prize_box", "show_paypal_box", "show_geo_notice", "require_age", "require_residency"):
            self.assertTrue(ctx[flag], flag)


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False)
class TermsPageWithTermsTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.client.force_login(self.user)

    def _get(self, tournament):
        return self.client.get(reverse("tournaments:money_terms", args=[tournament.pk]))

    def test_all_flags_on(self):
        t = _tournament(terms=_terms())
        resp = self._get(t)
        self.assertContains(resp, "Page Terms")
        self.assertContains(resp, "Terms of Participation &mdash; v2.3")
        self.assertContains(resp, "agree to the Terms &amp; Conditions v2.3 above")
        self.assertContains(resp, "<p>Prize 500.00 ILS</p>", html=True)
        for marker in ("cb_age", "cb_residency", "cb_terms", "Payout address on file", "Eligibility restriction"):
            self.assertContains(resp, marker)
        self.assertNotContains(resp, "Gauntlet")
        self.assertNotContains(resp, "v1.0")

    def test_flags_off_hide_boxes_and_checkboxes(self):
        t = _tournament(terms=_terms(
            requires_age_18=False, requires_israeli_residency=False, has_prize=False,
        ))
        resp = self._get(t)
        self.assertContains(resp, "cb_terms")
        for marker in ("cb_age", "cb_residency", "Payout address on file", "Eligibility restriction", "Prize: 500.00"):
            self.assertNotContains(resp, marker)

    def test_only_one_checkbox_flag(self):
        t = _tournament(terms=_terms(requires_age_18=True, requires_israeli_residency=False))
        resp = self._get(t)
        self.assertContains(resp, "cb_age")
        self.assertNotContains(resp, "cb_residency")

    def test_join_password_and_time_per_move_kept(self):
        t = _tournament(terms=_terms(), join_password="secret")
        resp = self._get(t)
        self.assertContains(resp, "join_password_input")
        self.assertContains(resp, "data-thinking-group")

    def test_body_context_is_limited_and_escaped(self):
        terms = _terms(body="{{ tournament.name }}|{{ request }}|{{ user }}|{{ terms.title }}")
        t = _tournament(name="<i>x</i>", terms=terms)
        self.assertEqual(
            str(build_terms_context(t)["terms_body"]), "&lt;i&gt;x&lt;/i&gt;|||Page Terms",
        )

    def test_include_and_extends_are_unavailable(self):
        for i, body in enumerate(('{% include "base.html" %}', '{% extends "base.html" %}')):
            terms = _terms(slug=f"x-{i}", body=body)
            with self.assertRaises(TemplateDoesNotExist):
                build_terms_context(_tournament(terms=terms))


class GauntletPageNamesTest(TestCase):
    def test_empty_state_is_neutral(self):
        resp = self.client.get(reverse("tournaments:gauntlet"))
        self.assertContains(resp, "No tournament has been run yet")
        self.assertNotContains(resp, "No gauntlet")

    def test_title_and_archive_use_tournament_names(self):
        champion = _user("champ")
        _tournament(
            name="The Gladiatormania Spring", type=Tournament.Type.GLADIATORMANIA,
            status=Tournament.Status.COMPLETED, champion=champion,
            start_time=timezone.now() - timedelta(days=9),
        )
        _tournament(
            name="Gladiator Gauntlet Week 4", status=Tournament.Status.OPEN,
            start_time=timezone.now() + timedelta(days=1),
        )
        resp = self.client.get(reverse("tournaments:gauntlet"))
        self.assertContains(resp, "<title>")
        self.assertContains(resp, "Gladiator Gauntlet Week 4")
        self.assertContains(resp, "<span>The Gladiatormania Spring</span>", html=True)
