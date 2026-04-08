"""Template filters for the threaded comments system."""

import re
from html import escape

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name="render_markdown")
def render_markdown(text):
    """Lightweight inline-Markdown → safe HTML.

    Supported syntax:
      **bold**  *italic*  `inline code`  [link text](url)
    All other HTML is escaped.
    """
    if not text:
        return ""
    # 1. Escape raw HTML
    text = escape(str(text))
    # 2. Inline code (before bold/italic so backtick content is untouched)
    text = re.sub(
        r"`([^`]+)`",
        r'<code class="bg-gray-700/60 text-green-300 px-1 py-0.5 rounded text-xs font-mono">\1</code>',
        text,
    )
    # 3. Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # 4. Italic (single *)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # 5. Links  [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" class="text-brand hover:underline" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    # 6. Newlines → <br>
    text = text.replace("\n", "<br>")
    return mark_safe(text)


@register.filter(name="add_int")
def add_int(value, arg):
    """Add two values as integers.  {{ depth|add_int:1 }}"""
    try:
        return int(value) + int(arg)
    except (ValueError, TypeError):
        return value
