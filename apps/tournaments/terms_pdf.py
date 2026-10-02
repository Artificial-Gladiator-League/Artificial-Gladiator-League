"""Convert a rendered TournamentTerms body into the PDF's (kind, markup) blocks.

kind: "h" heading | "p" paragraph | "s" indented sub-clause. Markup is reportlab
paragraph markup (only <b>, <i>, <br/> and escaped text).
"""
import html
import re
from html.parser import HTMLParser
from xml.sax.saxutils import escape

from .terms import render_body

_FLUSH_TAGS = {"hr", "ul", "ol"}
_HEADINGS = {"h1", "h2", "h3", "h4"}
_INLINE = {"strong": "b", "b": "b", "em": "i", "i": "i"}


class _BlockParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._kind = None
        self._parts = []

    def _open(self, kind):
        self._flush()
        self._kind = kind

    def _flush(self):
        if self._kind is not None:
            text = " ".join("".join(self._parts).split())
            if text:
                self.blocks.append((self._kind, text))
        self._kind = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "p":
            self._open("h" if "font-semibold" in classes else "s" if "pl-4" in classes else "p")
        elif tag in _HEADINGS:
            self._open("h")
        elif tag == "li":
            self._open("p")
            self._parts.append("&bull; ")
        elif tag in _INLINE:
            self._parts.append(f"<{_INLINE[tag]}>")
        elif tag == "br":
            self._parts.append("<br/>")
        elif tag in _FLUSH_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("p", "li") or tag in _HEADINGS:
            self._flush()
        elif tag in _INLINE:
            self._parts.append(f"</{_INLINE[tag]}>")

    def handle_data(self, data):
        if self._kind is None:
            if not data.strip():
                return
            self._open("p")
        self._parts.append(escape(data))

    def close(self):
        super().close()
        self._flush()


def html_to_blocks(rendered_html):
    parser = _BlockParser()
    parser.feed(rendered_html)
    parser.close()
    return parser.blocks


def _plain(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).lower()


def _renderable(char):
    # reportlab's built-in Helvetica covers WinAnsi (cp1252) only; anything else prints as a black square.
    try:
        char.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def pdf_safe_text(value, fallback):
    """*value* if every character can be drawn in the PDF font, else *fallback*."""
    value = value or ""
    return value if all(_renderable(c) for c in value) else fallback


def pdf_safe_markup(markup):
    """Replace each run of undrawable characters in paragraph markup with a single [?]."""
    out, in_run = [], False
    for c in markup:
        if _renderable(c):
            out.append(c)
            in_run = False
        elif not in_run:
            out.append("[?]")
            in_run = True
    return "".join(out)


def terms_pdf_blocks(terms, tournament):
    """PDF blocks for *terms*; the PDF prints its own title, so a leading title line is dropped."""
    blocks = html_to_blocks(render_body(terms, tournament))
    if blocks and blocks[0][0] == "h" and terms.title.lower() in _plain(blocks[0][1]):
        blocks = blocks[1:]
    return blocks
