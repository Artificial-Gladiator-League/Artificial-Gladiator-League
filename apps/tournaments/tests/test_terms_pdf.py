from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from reportlab import rl_config
from reportlab.platypus import Paragraph, SimpleDocTemplate

from apps.tournaments.models import Tournament, TournamentParticipant, TournamentTerms
from apps.tournaments.tasks import _build_tc_pdf
from apps.tournaments.terms import accepted_terms
from apps.tournaments.terms_pdf import html_to_blocks

User = get_user_model()
SEED = import_module("apps.tournaments.migrations.0031_seed_tournament_terms")
GOLDEN = Path(__file__).parent / "fixtures" / "legacy_tc.pdf"


def _terms(slug, version, title, body, **kw):
    defaults = dict(requires_age_18=True, requires_israeli_residency=True, has_prize=True)
    defaults.update(kw)
    return TournamentTerms.objects.create(slug=slug, version=version, title=title, body=body, **defaults)


def _tournament(name="Golden Cup", ttype=Tournament.Type.GAUNTLET, **kw):
    defaults = dict(
        name=name, type=ttype, status=Tournament.Status.OPEN,
        start_time=timezone.now() + timedelta(days=1), is_money_tournament=True,
        prize_amount=Decimal("500.00"), prize_currency="ILS", terms_version="",
    )
    defaults.update(kw)
    return Tournament.objects.create(**defaults)


def _paragraph_texts(tournament, user, seconds=5):
    captured = {}
    original = SimpleDocTemplate.build

    def spy(self, story, *args, **kwargs):
        captured["story"] = list(story)
        return original(self, story, *args, **kwargs)

    with mock.patch.object(SimpleDocTemplate, "build", spy):
        pdf = _build_tc_pdf(tournament, user, seconds)
    assert pdf.startswith(b"%PDF")
    return [f.text for f in captured["story"] if isinstance(f, Paragraph)]


class HtmlToBlocksTest(SimpleTestCase):
    def test_headings_paragraphs_subclauses_bullets(self):
        blocks = html_to_blocks(
            '<p class="font-semibold text-gray-200 mb-1">1. Definitions</p>'
            '<p>1.1 <strong>&ldquo;AGL&rdquo;</strong> means us &amp; them.</p>'
            '<p class="pl-4">a. Be at least <strong>18</strong>;</p>'
            '<hr class="x"><ul><li>First <em>point</em></li><li>Second</li></ul>'
        )
        self.assertEqual(blocks, [
            ("h", "1. Definitions"),
            ("p", "1.1 <b>\u201cAGL\u201d</b> means us &amp; them."),
            ("s", "a. Be at least <b>18</b>;"),
            ("p", "&bull; First <i>point</i>"),
            ("p", "&bull; Second"),
        ])

    def test_text_is_escaped_for_reportlab(self):
        self.assertEqual(html_to_blocks("<p>1 &lt; 2 &amp; <b>x</b></p>"), [("p", "1 &lt; 2 &amp; <b>x</b>")])

    def test_whitespace_collapsed_and_stray_text_kept(self):
        self.assertEqual(html_to_blocks("loose\n   text <p> a \n b </p>"), [("p", "loose text"), ("p", "a b")])


class LegacyPdfUnchangedTest(TestCase):
    def test_no_terms_output_is_byte_identical_to_today(self):
        t = _tournament()
        user = User.objects.create_user("golden", password="x")
        self.assertIsNone(accepted_terms(t, user))
        with mock.patch.object(rl_config, "invariant", 1):
            pdf = _build_tc_pdf(t, user, 5)
        self.assertEqual(pdf, GOLDEN.read_bytes())

    def test_a_participant_without_stored_terms_and_no_tournament_terms_stays_legacy(self):
        t = _tournament()
        user = User.objects.create_user("p1", password="x")
        TournamentParticipant.objects.create(tournament=t, user=user)
        self.assertIsNone(accepted_terms(t, user))
        texts = _paragraph_texts(t, user)
        self.assertEqual(texts[0], "The Gladiator Gauntlet &mdash; Terms &amp; Conditions")
        self.assertTrue(any(x.startswith("<b>Organizer:</b>") for x in texts))


class RecordBasedPdfTest(TestCase):
    def setUp(self):
        self.gauntlet = _terms("pdf-gauntlet", "1.0", "Gladiator Gauntlet", SEED.GAUNTLET_BODY)
        self.mania = _terms("pdf-mania", "1.0", "The Gladiatormania", SEED.MANIA_BODY)
        self.user = User.objects.create_user("reg", password="x")

    def _register(self, tournament, terms):
        return TournamentParticipant.objects.create(
            tournament=tournament, user=self.user,
            accepted_terms_slug=terms.slug, terms_version_accepted=terms.version,
        )

    def test_gauntlet_registrant_gets_gauntlet_branding_from_the_record(self):
        t = _tournament(terms=self.gauntlet)
        self._register(t, self.gauntlet)
        texts = _paragraph_texts(t, self.user)
        self.assertEqual(texts[0], "Gladiator Gauntlet &mdash; Terms &amp; Conditions")
        self.assertIn("Version: 1.0", texts)
        self.assertTrue(any("The Gladiator Gauntlet is a" in x for x in texts))

    def test_gladiatormania_registrant_sees_name_and_version_and_no_gauntlet(self):
        t = _tournament(name="Mania Week 1", ttype=Tournament.Type.GLADIATORMANIA, terms=self.mania)
        self._register(t, self.mania)
        texts = _paragraph_texts(t, self.user)
        self.assertEqual(texts[0], "The Gladiatormania &mdash; Terms &amp; Conditions")
        self.assertIn("Version: 1.0", texts)
        self.assertIn("Tournament: Mania Week 1", texts)
        self.assertFalse([x for x in texts if "Gauntlet" in x])
        # Layout: prize line, organizer from the body, then sections.
        self.assertTrue(any(x.startswith("<b>Prize:</b> 500.00 ILS") for x in texts))
        self.assertEqual(sum(x.startswith("<b>Organizer:</b>") for x in texts), 1)
        self.assertIn("Key Points (Summary)", texts)
        self.assertTrue(any(x.startswith("5.1 ") and "500.00 ILS" in x for x in texts))
        self.assertEqual(sum("Terms &amp; Conditions" in x and x.startswith("The Gladiatormania") for x in texts), 1)

    def test_participant_who_accepted_v1_0_still_gets_v1_0(self):
        v10 = _terms("versioned", "1.0", "Versioned", "<p>OLD WORDING</p>")
        t = _tournament(terms=v10)
        self._register(t, v10)

        v11 = _terms("versioned", "1.1", "Versioned", "<p>NEW WORDING</p>")
        TournamentTerms.objects.filter(pk=v10.pk).update(is_active=False)
        t.terms = v11
        t.save()

        texts = _paragraph_texts(t, self.user)
        self.assertIn("Version: 1.0", texts)
        self.assertIn("OLD WORDING", texts)
        self.assertNotIn("NEW WORDING", texts)
        self.assertEqual(accepted_terms(t, self.user), v10)

    def test_falls_back_to_the_tournament_terms_when_nothing_is_stored(self):
        v11 = _terms("fallback", "1.1", "Fallback", "<p>CURRENT WORDING</p>")
        t = _tournament(terms=v11)
        TournamentParticipant.objects.create(tournament=t, user=self.user)
        texts = _paragraph_texts(t, self.user)
        self.assertIn("Version: 1.1", texts)
        self.assertIn("CURRENT WORDING", texts)

    def test_prize_line_follows_has_prize(self):
        terms = _terms("noprize", "1.0", "No Prize", "<p>Body</p>", has_prize=False)
        t = _tournament(terms=terms, prize_amount=None)
        self._register(t, terms)
        self.assertFalse([x for x in _paragraph_texts(t, self.user) if x.startswith("<b>Prize:</b>")])

    def test_title_with_markup_characters_is_escaped(self):
        terms = _terms("amp", "1.0", "Tom & <Jerry>", "<p>Body</p>")
        t = _tournament(terms=terms, prize_amount=None, is_money_tournament=False)
        self._register(t, terms)
        self.assertEqual(_paragraph_texts(t, self.user)[0], "Tom &amp; &lt;Jerry&gt; &mdash; Terms &amp; Conditions")
