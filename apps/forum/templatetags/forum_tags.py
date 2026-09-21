"""Template filters for the forum app.

Provides `render_markdown` (safe HTML from a Markdown subset, no external
dependency) and `multiply` (integer arithmetic for reply indentation).
"""
import re

from django import template
from django.utils.safestring import mark_safe
from html import escape

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
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" class="text-purple hover:text-gold underline transition" target="_blank" rel="noopener">\1</a>',
        text,
    )
    text = re.sub(
        r'(?<!href=")(https?://\S+)',
        r'<a href="\1" class="text-purple hover:text-gold underline transition" target="_blank" rel="noopener">\1</a>',
        text,
    )
    # Wrap paragraphs (blank-line separated), leaving <pre> blocks untouched.
    paragraphs = re.split(r"\n{2,}", text)
    paragraphs = [
        p if p.strip().startswith("<pre") else f"<p>{p}</p>"
        for p in paragraphs
    ]
    text = "\n".join(paragraphs)
    text = re.sub(r"(?<!</p>)\n(?!<)", "<br>", text)
    return text


@register.filter(name="render_markdown")
def render_markdown(value):
    """Render a markdown string to safe HTML."""
    return mark_safe(_md_to_html(str(value)))


@register.filter(name="multiply")
def multiply(value, arg):
    """Multiply value by arg — used for indentation.

    Usage: {{ post.indent|multiply:24 }}  →  "48" (for 2 levels x 24px)
    """
    try:
        return int(value) * int(arg)
    except (ValueError, TypeError):
        return 0
