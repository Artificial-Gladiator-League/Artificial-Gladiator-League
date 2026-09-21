"""Template filters for the news app.

Provides `render_markdown` — a subset-of-Markdown -> safe HTML renderer with
no external dependency (mirrors apps/forum/templatetags/forum_tags.py). All
raw HTML in the source text is escaped before any tag is generated, so this
is safe to mark_safe even though posts are staff-authored.
"""
import re
from html import escape

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


def _code_block(m):
    lang = m.group(1)
    lang_class = f' class="language-{lang}"' if lang else ""
    return f'<pre class="bg-surfaceLight rounded-lg p-3 overflow-x-auto my-2 text-xs"><code{lang_class}>{m.group(2)}</code></pre>'


def _md_to_html(text):
    """Convert a subset of Markdown to HTML (no external deps)."""
    text = escape(text)
    text = re.sub(r"```(\w*)\n(.*?)```", _code_block, text, flags=re.DOTALL)
    text = re.sub(
        r"`([^`]+)`",
        r'<code class="bg-surfaceLight px-1.5 py-0.5 rounded text-xs text-purple font-mono">\1</code>',
        text,
    )
    text = re.sub(r"^#{3}\s+(.+)$", r"<h4 class=\"font-semibold mt-3 mb-1\">\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{2}\s+(.+)$", r"<h3 class=\"font-bold text-lg mt-4 mb-2\">\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{1}\s+(.+)$", r"<h2 class=\"font-bold text-xl mt-4 mb-2\">\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" class="text-brand hover:text-gold underline transition" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    text = re.sub(
        r'(?<!href=")(?<!>)(https?://\S+)',
        r'<a href="\1" class="text-brand hover:text-gold underline transition" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    # Wrap paragraphs (blank-line separated), leaving block-level tags untouched.
    paragraphs = re.split(r"\n{2,}", text)
    paragraphs = [
        p if re.match(r"^\s*<(pre|h2|h3|h4)", p) else f"<p>{p}</p>"
        for p in paragraphs
    ]
    text = "\n".join(paragraphs)
    text = re.sub(r"(?<!</p>)\n(?!<)", "<br>", text)
    return text


@register.filter(name="render_markdown")
def render_markdown(value):
    """Render a markdown string to safe HTML."""
    if not value:
        return ""
    return mark_safe(_md_to_html(str(value)))
