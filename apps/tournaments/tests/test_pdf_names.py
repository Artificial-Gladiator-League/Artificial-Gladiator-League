from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.tournaments.models import Tournament, TournamentParticipant
from apps.tournaments.terms_pdf import pdf_safe_markup, pdf_safe_text
from apps.tournaments.tests.test_terms_pdf import _paragraph_texts, _terms, _tournament

User = get_user_model()

DEVANAGARI = "\u0930\u092e\u0947\u0936"        # Ramesh
HEBREW = "\u05d3\u05e0\u05d4"                  # Dana


def _drawable(text):
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


class SafeTextTest(SimpleTestCase):
    def test_text_the_font_can_draw_is_kept(self):
        for value in ("Dana", "Jos\u00e9", "Zo\u00eb \u2014 Cup", "R&D 2026"):
            with self.subTest(value=value):
                self.assertEqual(pdf_safe_text(value, "FALLBACK"), value)

    def test_devanagari_hebrew_mixed_and_invisible_marks_use_the_fallback(self):
        for value in (DEVANAGARI, HEBREW, f"Dana {HEBREW}", f"{DEVANAGARI}_99", "\u200fDana", "\u0928\u092e\u0938\u094d\u0924\u0947 Cup"):
            with self.subTest(value=value):
                self.assertEqual(pdf_safe_text(value, "FALLBACK"), "FALLBACK")

    def test_markup_runs_become_one_placeholder_and_tags_survive(self):
        self.assertEqual(
            pdf_safe_markup(f"<b>Hello</b> {DEVANAGARI} and {HEBREW}{DEVANAGARI} &amp; done"),
            "<b>Hello</b> [?] and [?] &amp; done",
        )
        self.assertEqual(pdf_safe_markup("plain &mdash; text \u2014 \u2019"), "plain &mdash; text \u2014 \u2019")


class PdfNamesTest(TestCase):
    def _build(self, tournament_name, username, terms=None):
        t = _tournament(name=tournament_name, ttype=Tournament.Type.GLADIATORMANIA if terms else Tournament.Type.GAUNTLET, terms=terms)
        user = User.objects.create_user(username, password="x")
        if terms:
            TournamentParticipant.objects.create(
                tournament=t, user=user, accepted_terms_slug=terms.slug, terms_version_accepted=terms.version,
            )
        return t, user, _paragraph_texts(t, user)

    def test_devanagari_and_hebrew_names_show_ids_not_black_squares(self):
        for tournament_name, username in (
            (f"{DEVANAGARI} Cup", DEVANAGARI),
            (f"{HEBREW} Cup", HEBREW),
            ("Golden Cup", f"Dana{HEBREW}"),
        ):
            with self.subTest(tournament_name=tournament_name, username=username):
                t, user, texts = self._build(tournament_name, username)
                self.assertTrue(all(_drawable(x) for x in texts), texts)
                if not _drawable(username):
                    self.assertIn(f"Participant: Account #{user.pk} | Accepted at registration for", " ".join(texts))
                if not _drawable(tournament_name):
                    self.assertIn(f"Tournament: Tournament #{t.pk}", texts)

    def test_ordinary_names_are_unchanged_and_now_escaped(self):
        t, user, texts = self._build("R&D <Cup>", "dana")
        self.assertIn("Tournament: R&amp;D &lt;Cup&gt;", texts)
        self.assertIn(f"Participant: dana | Accepted at registration for R&amp;D &lt;Cup&gt;.", texts)

    def test_terms_record_with_non_latin_text_prints_placeholders(self):
        terms = _terms("mixed", "1.0", f"Terms {HEBREW}", f"<p>1. Intro</p><p>Contact {DEVANAGARI} at AGL.</p>")
        t, user, texts = self._build("Golden Cup", "dana", terms=terms)
        self.assertTrue(all(_drawable(x) for x in texts), texts)
        self.assertIn("Tournament &mdash; Terms &amp; Conditions", texts)
        self.assertIn("Contact [?] at AGL.", texts)
