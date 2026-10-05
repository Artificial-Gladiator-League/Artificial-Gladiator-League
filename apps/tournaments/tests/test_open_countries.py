"""Tournaments open to all countries (empty allowed_countries) and the QA country/terms test bench."""
from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.tournaments.admin import TournamentAdmin
from apps.tournaments.countries import countries_phrase, is_open_to_all
from apps.tournaments.eligibility import UNVERIFIED_LOCATION_MESSAGE, check_geo_eligibility, geo_block_message
from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.profile_country import (
    MISSING, NOT_ALLOWED, NOT_ELIGIBLE_NOTICE, profile_country_verdict,
)
from apps.tournaments.tests.test_gladiatormania import _external_patches, _players
from apps.tournaments.tests.test_gladiatormania import _terms as _mania_terms
from apps.tournaments.tests.test_gladiatormania import _tournament as _make_tournament
from apps.tournaments.tests.test_terms_join import PASS_GATES, JoinTermsTestBase
from apps.tournaments.tests.test_terms_join import _terms as _join_terms
from apps.tournaments.tests.test_terms_join import _tournament as _join_tournament
from apps.tournaments.tests.test_terms_pdf import _paragraph_texts

User = get_user_model()
LOOKUP = "apps.tournaments.eligibility.lookup_country"
PUBLIC_IP = "194.90.0.1"
ALL = {"terms_accepted": "1", "confirmed_age_18_plus": "1", "confirmed_residency": "1"}


def _set_country(user, code):
    User.objects.filter(pk=user.pk).update(country=code, country_locked=bool(code))
    user.refresh_from_db()


def _request(ip=PUBLIC_IP):
    return RequestFactory().get("/", HTTP_X_FORWARDED_FOR=ip)


class OpenToAllHelperTest(SimpleTestCase):
    def test_only_a_real_empty_list_means_all_countries(self):
        self.assertTrue(is_open_to_all([]))
        self.assertTrue(is_open_to_all(()))
        for value in (None, "", {}, ["IL"], ["IN", "IL"], [""], "all"):
            with self.subTest(value=value):
                self.assertFalse(is_open_to_all(value))

    def test_phrase_is_never_empty(self):
        self.assertEqual(countries_phrase([]), "all countries")
        self.assertEqual(countries_phrase(["IN"]), "India")
        self.assertEqual(countries_phrase(["IN", "IL"]), "India and Israel")


class OpenToAllModelRulesTest(TestCase):
    def setUp(self):
        self.base_terms = _mania_terms("open-rules")

    def _t(self, ttype=Tournament.Type.GAUNTLET, **kw):
        defaults = dict(
            name="Open", type=ttype, start_time=timezone.now() + timedelta(days=1),
            is_money_tournament=True, prize_amount=Decimal("500.00"), prize_currency="INR",
            allowed_countries=[], terms=self.base_terms,
        )
        defaults.update(kw)
        return Tournament(**defaults)

    def test_gauntlet_gladiatormania_and_qa_accept_an_empty_list(self):
        for ttype in Tournament.Type.values:
            with self.subTest(type=ttype):
                t = self._t(ttype, terms=_mania_terms(f"open-{ttype}"))
                t.clean()
                self.assertEqual(t.allowed_countries, [])
                self.assertTrue(t.is_open_to_all)

    def test_restricted_lists_are_valid_for_every_type(self):
        for ttype in Tournament.Type.values:
            with self.subTest(type=ttype):
                self._t(ttype, allowed_countries=["IN"], terms=_mania_terms(f"restricted-{ttype}")).clean()

    def test_junk_is_not_treated_as_all_countries(self):
        for value in ("", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    self._t(allowed_countries=value, prize_amount=None, terms=None, is_money_tournament=False).clean()
                self.assertIn("at least one", " ".join(ctx.exception.message_dict["allowed_countries"]))

    def test_open_tournament_properties(self):
        t = self._t()
        self.assertFalse(t.is_israel_only)
        self.assertTrue(t.requires_legal_check)
        self.assertEqual(t.allowed_country_codes, [])
        self.assertEqual(t.allowed_countries_text, "all countries")
        self.assertEqual(t.residents_only_label, "Open to all countries")

    def test_money_tournament_open_beyond_israel_still_needs_a_terms_record(self):
        t = self._t(terms=None, prize_amount=None)
        with self.assertRaises(ValidationError) as ctx:
            t.clean()
        self.assertIn("needs a terms record", " ".join(ctx.exception.message_dict["terms"]))

    def test_ils_prize_is_rejected_like_any_tournament_beyond_israel(self):
        with self.assertRaises(ValidationError) as ctx:
            self._t(prize_currency="ILS").clean()
        self.assertIn("prize_currency", ctx.exception.message_dict)

    def test_open_to_all_is_chess_only_because_it_includes_india(self):
        with self.assertRaises(ValidationError) as ctx:
            self._t(game_type=Tournament.GameType.BREAKTHROUGH).clean()
        self.assertIn("includes India", " ".join(ctx.exception.message_dict["game_type"]))
        self._t(game_type=Tournament.GameType.CHESS).clean()

    def test_terms_do_not_need_a_residency_flag_when_open_to_all(self):
        self._t(terms=_mania_terms("open-no-flag", requires_israeli_residency=False)).clean()
        with self.assertRaises(ValidationError) as ctx:
            self._t(allowed_countries=["IN"], terms=_mania_terms("in-no-flag", requires_israeli_residency=False)).clean()
        self.assertIn("residency declaration", " ".join(ctx.exception.message_dict["terms"]))

    def test_qa_can_carry_a_terms_record(self):
        self._t(Tournament.Type.QA, allowed_countries=["IN"], terms=_mania_terms("qa-terms")).clean()


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
class GeoGateOpenTest(SimpleTestCase):
    def test_open_list_never_looks_the_ip_up(self):
        with mock.patch(LOOKUP) as lookup:
            self.assertEqual(check_geo_eligibility(_request(), []), (PUBLIC_IP, None, None))
        lookup.assert_not_called()

    def test_open_list_does_not_fail_closed_when_the_lookup_service_is_down(self):
        with mock.patch(LOOKUP, return_value=None) as lookup:
            self.assertEqual(check_geo_eligibility(_request(), []), (PUBLIC_IP, None, None))
        lookup.assert_not_called()

    def test_restricted_lists_are_still_enforced(self):
        with mock.patch(LOOKUP, return_value="US"):
            self.assertEqual(check_geo_eligibility(_request(), ["IN"]), (PUBLIC_IP, "US", False))
        with mock.patch(LOOKUP, return_value=None):
            self.assertEqual(check_geo_eligibility(_request(), ["IN"]), (PUBLIC_IP, None, False))

    def test_block_message_never_names_an_empty_country_list(self):
        message = geo_block_message([], "US")
        self.assertEqual(message, UNVERIFIED_LOCATION_MESSAGE)
        self.assertNotIn("residents of  ", message)


class ProfileVerdictOpenTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pv", password="pw")

    def _t(self, countries, slug):
        return _make_tournament(
            Tournament.Type.GAUNTLET, slug, allowed_countries=countries, prize_currency="INR",
            terms=_mania_terms(slug), capacity=16, rounds_total=5,
        )

    def test_open_tournament_never_objects(self):
        t = self._t([], "pv-open")
        self.assertIsNone(profile_country_verdict(self.user, t))
        for code in ("US", "IN", "IL"):
            _set_country(self.user, code)
            self.assertIsNone(profile_country_verdict(self.user, t), code)

    def test_restricted_gauntlet_enforces_its_codes(self):
        t = self._t(["IN"], "pv-in")
        self.assertEqual(profile_country_verdict(self.user, t).kind, MISSING)
        _set_country(self.user, "US")
        verdict = profile_country_verdict(self.user, t)
        self.assertEqual((verdict.kind, verdict.message), (NOT_ALLOWED, NOT_ELIGIBLE_NOTICE))
        _set_country(self.user, "IN")
        self.assertIsNone(profile_country_verdict(self.user, t))


class OpenToAllJoinFlowTest(JoinTermsTestBase):
    """The real join path of a Gauntlet that nobody restricted."""

    def _open(self, **kw):
        terms = kw.pop("terms", None) or _join_terms(slug="open-join", requires_israeli_residency=True)
        return _join_tournament(allowed_countries=[], prize_currency="INR", terms=terms, **kw), terms

    def test_terms_page_has_neutral_wording_and_no_residency_box(self):
        t, _ = self._open()
        resp = self.client.get(reverse("tournaments:money_terms", args=[t.pk]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("players from all countries", html)
        self.assertNotIn("cb_residency", html)
        self.assertNotRegex(html, r"residents? of\s+(only|\.|<)")
        self.assertNotIn("resident of  ", html)

    def test_player_joins_without_any_residency_declaration(self):
        t, terms = self._open()
        resp = self.post_join(t, self.shown(terms, terms_accepted="1", confirmed_age_18_plus="1"))
        self.assertEqual(resp.status_code, 302)
        row = TournamentParticipant.objects.get(tournament=t, user=self.user)
        self.assertEqual(row.declared_residency_countries, "")
        self.assertIsNone(row.geo_eligible)

    def test_no_profile_country_is_required(self):
        t, terms = self._open()
        self.assertEqual(self.client.get(reverse("tournaments:money_terms", args=[t.pk])).status_code, 200)
        self.assertEqual(User.objects.get(pk=self.user.pk).country, "")

    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
    def test_public_ip_joins_without_a_lookup_even_if_the_service_is_down(self):
        t, terms = self._open()
        with PASS_GATES[0], PASS_GATES[1], mock.patch(LOOKUP, return_value=None) as lookup:
            resp = self.client.post(
                reverse("tournaments:join", args=[t.pk]),
                data=self.shown(terms, terms_accepted="1", confirmed_age_18_plus="1"),
                HTTP_X_FORWARDED_FOR=PUBLIC_IP,
            )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.joined(t))
        lookup.assert_not_called()

    def test_detail_and_list_say_open_to_all(self):
        t, _ = self._open()
        for url in (reverse("tournaments:detail", args=[t.pk]), reverse("tournaments:list")):
            html = self.client.get(url).content.decode()
            self.assertIn("Open to all countries", html, url)
            self.assertNotIn("Open to residents of  ", html, url)


class RestrictedGauntletTest(JoinTermsTestBase):
    @override_settings(MONEY_TOURNAMENT_GEO_ENABLED=True)
    def test_ip_gate_enforces_the_listed_codes(self):
        terms = _join_terms(slug="restricted-ip")
        t = _join_tournament(allowed_countries=["IN"], prize_currency="INR", terms=terms)
        self.set_country("IN")  # the profile gate passes, so only the IP decides
        data = self.shown(terms, **ALL)
        with PASS_GATES[0], PASS_GATES[1], mock.patch(LOOKUP, return_value="US"):
            self.client.post(reverse("tournaments:join", args=[t.pk]), data=data, HTTP_X_FORWARDED_FOR=PUBLIC_IP)
        self.assertFalse(self.joined(t))
        with PASS_GATES[0], PASS_GATES[1], mock.patch(LOOKUP, return_value="IN"):
            self.client.post(reverse("tournaments:join", args=[t.pk]), data=data, HTTP_X_FORWARDED_FOR=PUBLIC_IP)
        self.assertTrue(self.joined(t))

    def test_profile_country_gate_blocks_with_the_notice(self):
        terms = _join_terms(slug="restricted-profile")
        t = _join_tournament(allowed_countries=["IN"], prize_currency="INR", terms=terms)
        self.set_country("US")
        resp = self.client.post(reverse("tournaments:join", args=[t.pk]), data=self.shown(terms, **ALL), follow=True)
        self.assertFalse(self.joined(t))
        self.assertContains(resp, NOT_ELIGIBLE_NOTICE)


@override_settings(MONEY_TOURNAMENT_GEO_ENABLED=False, HF_PLATFORM_TOKEN="hf_test")
class QaCountryBenchTest(TestCase):
    """Two QA players: one inside the allowed countries, one outside."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        _external_patches(stack, confirmation_email=True)
        self.allowed, self.restricted = _players(email=True)
        _set_country(self.allowed, "IN")
        _set_country(self.restricted, "US")
        self.terms = _mania_terms(
            "qa-bench", title="Bench Terms", body="<p>Bench clause alpha for {{ tournament.name }}</p>",
        )
        self.t = _make_tournament(
            Tournament.Type.QA, "QA Bench", terms=self.terms, allowed_countries=["IN"], prize_currency="INR",
        )

    @staticmethod
    def _client(user):
        client = Client()
        client.force_login(user)
        return client

    def _data(self, terms=None, **extra):
        terms = terms or self.terms
        return {"terms_slug": terms.slug, "terms_version": terms.version, **ALL, **extra}

    def _join(self, user, tournament=None, terms=None, **extra):
        tournament = tournament or self.t
        return self._client(user).post(
            reverse("tournaments:join", args=[tournament.pk]), data=self._data(terms, **extra),
        )

    def _joined(self, user, tournament=None):
        return TournamentParticipant.objects.filter(tournament=tournament or self.t, user=user).exists()

    def test_the_bench_tournament_is_valid_and_keeps_its_qa_limits(self):
        self.t.refresh_from_db()
        self.t.full_clean()
        self.assertEqual((self.t.capacity, self.t.rounds_total), (2, 1))

    def test_restricted_player_sees_the_notice_before_registering(self):
        resp = self._client(self.restricted).get(reverse("tournaments:detail", args=[self.t.pk]))
        self.assertContains(resp, NOT_ELIGIBLE_NOTICE)
        self.assertContains(resp, 'id="country-notice"')
        self.assertNotContains(resp, reverse("tournaments:money_terms", args=[self.t.pk]))

    def test_allowed_player_sees_no_notice_and_a_register_link(self):
        resp = self._client(self.allowed).get(reverse("tournaments:detail", args=[self.t.pk]))
        self.assertNotContains(resp, 'id="country-notice"')
        self.assertContains(resp, reverse("tournaments:money_terms", args=[self.t.pk]))

    def test_restricted_player_cannot_reach_the_terms_page_and_sees_the_message(self):
        client = self._client(self.restricted)
        resp = client.get(reverse("tournaments:money_terms", args=[self.t.pk]))
        self.assertRedirects(resp, reverse("tournaments:detail", args=[self.t.pk]), fetch_redirect_response=False)
        followed = client.get(reverse("tournaments:detail", args=[self.t.pk]))
        self.assertContains(followed, NOT_ELIGIBLE_NOTICE)

    def test_blocked_join_redirects_to_detail_where_the_flash_message_is_rendered(self):
        resp = self._client(self.restricted).post(
            reverse("tournaments:join", args=[self.t.pk]), data=self._data(), follow=True,
        )
        self.assertFalse(self._joined(self.restricted))
        self.assertEqual(resp.redirect_chain[-1][0], reverse("tournaments:detail", args=[self.t.pk]))
        self.assertContains(resp, "lp-flash")
        self.assertContains(resp, "lp-msg--error")
        self.assertContains(resp, NOT_ELIGIBLE_NOTICE)

    def test_restricted_player_cannot_accept_terms_even_with_a_crafted_post(self):
        client = self._client(self.restricted)
        client.post(reverse("tournaments:money_terms", args=[self.t.pk]), data={"terms_accepted": "1"})
        self._join(self.restricted)
        self.assertFalse(self._joined(self.restricted))
        row = TournamentParticipant.objects.filter(tournament=self.t, user=self.restricted)
        self.assertFalse(row.exists())

    def test_allowed_player_joins_through_the_real_money_flow(self):
        client = self._client(self.allowed)
        page = client.get(reverse("tournaments:money_terms", args=[self.t.pk]))
        self.assertContains(page, "Bench clause alpha for QA Bench")
        self.assertEqual(self._join(self.allowed).status_code, 302)
        row = TournamentParticipant.objects.get(tournament=self.t, user=self.allowed)
        self.assertEqual((row.accepted_terms_slug, row.terms_version_accepted), ("qa-bench", "1.0"))
        self.assertEqual(row.profile_country_code, "IN")
        self.assertEqual(self.t.participants.count(), 1)

    def test_terms_page_and_acceptance_follow_the_admin_link(self):
        beta = _mania_terms("qa-bench-beta", title="Beta Terms", body="<p>Bench clause beta</p>")
        client = self._client(self.allowed)
        self.assertContains(client.get(reverse("tournaments:money_terms", args=[self.t.pk])), "clause alpha")

        Tournament.objects.filter(pk=self.t.pk).update(terms=beta)
        page = client.get(reverse("tournaments:money_terms", args=[self.t.pk]))
        self.assertContains(page, "Bench clause beta")
        self.assertNotContains(page, "clause alpha")
        self.assertContains(page, 'value="qa-bench-beta"')

        # A form that still carries the old record is refused; the new one is accepted and stored.
        self._join(self.allowed, terms=self.terms)
        self.assertFalse(self._joined(self.allowed))
        self._join(self.allowed, terms=beta)
        row = TournamentParticipant.objects.get(tournament=self.t, user=self.allowed)
        self.assertEqual((row.accepted_terms_slug, row.terms_version_accepted), ("qa-bench-beta", "1.0"))

    def test_confirmation_pdf_matches_the_terms_each_player_accepted(self):
        both = _make_tournament(
            Tournament.Type.QA, "QA Both", terms=self.terms, allowed_countries=["IN", "US"], prize_currency="INR",
        )
        with mock.patch("apps.tournaments.engine.start_tournament"):
            self._join(self.allowed, tournament=both)
            self._join(self.restricted, tournament=both)
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(len(message.attachments), 1)
            self.assertTrue(message.attachments[0][1].startswith(b"%PDF"))

        beta = _mania_terms("qa-bench-pdf", title="Pdf Beta", body="<p>Clause beta only</p>")
        Tournament.objects.filter(pk=both.pk).update(terms=beta)
        both.refresh_from_db()
        for user in (self.allowed, self.restricted):
            texts = _paragraph_texts(both, user)
            self.assertEqual(texts[0], "Bench Terms &mdash; Terms &amp; Conditions")
            self.assertTrue(any("Bench clause alpha" in x for x in texts))
            self.assertFalse(any("Clause beta" in x for x in texts))

    def test_pdf_uses_the_linked_record_for_a_player_with_nothing_stored(self):
        late = User.objects.create_user("late", password="pw")
        TournamentParticipant.objects.create(tournament=self.t, user=late)
        texts = _paragraph_texts(self.t, late)
        self.assertEqual(texts[0], "Bench Terms &mdash; Terms &amp; Conditions")


class CreateQaTournamentCommandTest(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("create_qa_tournament", *args, stdout=out)
        return out.getvalue()

    def _terms(self, slug="cmd-terms", **kw):
        return _mania_terms(slug, **kw)

    def test_default_is_the_old_israel_only_free_qa(self):
        out = self._run()
        t = Tournament.objects.get()
        self.assertEqual((t.type, t.allowed_countries, t.is_money_tournament), (Tournament.Type.QA, ["IL"], False))
        self.assertIn(reverse("tournaments:detail", args=[t.pk]), out)

    def test_all_countries(self):
        self._run("--allowed-countries", "all")
        self.assertEqual(Tournament.objects.get().allowed_countries, [])

    def test_money_bench_with_terms_prize_and_countries(self):
        terms = self._terms()
        out = self._run(
            "--allowed-countries", "IL,IN", "--money", "--terms-slug", terms.slug,
            "--prize-amount", "100", "--currency", "INR",
        )
        t = Tournament.objects.get()
        self.assertEqual(t.allowed_countries, ["IL", "IN"])
        self.assertTrue(t.is_money_tournament)
        self.assertEqual((t.terms, t.prize_amount, t.prize_currency), (terms, Decimal("100"), "INR"))
        self.assertEqual((t.capacity, t.rounds_total), (2, 1))
        self.assertIn(reverse("tournaments:money_terms", args=[t.pk]), out)
        self.assertIn(reverse("admin:tournaments_tournament_change", args=[t.pk]), out)

    def test_invalid_combinations_are_refused_and_nothing_is_created(self):
        terms = self._terms()
        bad = [
            ("--allowed-countries", "ZZ"),
            ("--allowed-countries", " , "),
            ("--money",),                                                   # no prize amount
            ("--terms-slug", "does-not-exist"),
            ("--allowed-countries", "IN", "--money", "--terms-slug", terms.slug, "--prize-amount", "100"),  # ILS
            ("--allowed-countries", "IN", "--money", "--prize-amount", "100", "--currency", "INR"),         # no terms
            ("--prize-amount", "abc"),
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(CommandError):
                self._run(*args)
        self.assertFalse(Tournament.objects.exists())


class AdminOpenToAllTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("root", "r@x.com", "pw")
        self.client.force_login(self.staff)

    def test_new_gauntlet_form_starts_open_to_all(self):
        resp = self.client.get(reverse("admin:tournaments_tournament_add"))
        self.assertEqual(resp.context["adminform"].form.initial.get("allowed_countries"), [])

    def test_other_types_keep_the_israel_default(self):
        resp = self.client.get(reverse("admin:tournaments_tournament_add") + "?type=qa")
        self.assertNotEqual(resp.context["adminform"].form.initial.get("allowed_countries"), [])

    def test_countries_column(self):
        model_admin = TournamentAdmin(Tournament, admin.site)
        self.assertEqual(model_admin.countries_display(Tournament(allowed_countries=[])), "All countries")
        self.assertEqual(model_admin.countries_display(Tournament(allowed_countries=["IN", "IL"])), "IN, IL")

    def test_blank_countries_saves_an_empty_list_for_a_gauntlet(self):
        terms = _mania_terms("admin-open")
        request = RequestFactory().get("/")
        request.user = self.staff
        form_class = TournamentAdmin(Tournament, admin.site).get_form(request)
        start = timezone.now() + timedelta(days=2)
        form = form_class({
            "name": "Open Gauntlet", "type": "gauntlet", "game_type": "chess", "time_control": "3+1",
            "capacity": 16, "rounds_total": 5, "current_round": 0, "status": "open",
            "start_time_0": start.strftime("%Y-%m-%d"), "start_time_1": start.strftime("%H:%M:%S"),
            "is_money_tournament": "on", "prize_amount": "500.00", "prize_currency": "INR",
            "payout_status": "pending", "payout_method": "paypal", "claim_deadline_days": 30,
            "terms": terms.pk, "allowed_countries": "",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.allowed_countries, [])

    def test_admin_script_sets_type_aware_country_defaults(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[3] / "static" / "admin" / "js" / "tournament_type_fields.js").read_text(
            encoding="utf-8",
        )
        self.assertIn("COUNTRY_DEFAULTS", js)
        self.assertIn("gauntlet: ''", js)
        self.assertIn("qa: 'IL'", js)
