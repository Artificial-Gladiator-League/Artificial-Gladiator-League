"""Template filters for the forum app.

Provides `render_markdown` (safe HTML from Markdown) and `multiply` (integer
arithmetic for indentation levels).
"""
import re

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


# ─── Lightweight Markdown-to-HTML ────────────────
# Covers: fenced code blocks, inline code, bold, italic, links, line breaks.
# For production, swap this for `markdown` or `mistune` library.

def _md_to_html(text: str) -> str:
    """Convert a subset of Markdown to HTML (no external deps)."""
    import html as _html

    text = _html.escape(text)

    # Fenced code blocks:  ```lang\n...\n```
    def _code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        lang_attr = f' class="language-{lang}"' if lang else ""
        return (
            f'<pre class="bg-surfaceLight rounded-lg p-3 overflow-x-auto my-2 text-xs">'
            f"<code{lang_attr}>{code}</code></pre>"
        )

    text = re.sub(
        r"```(\w*)\n(.*?)```",
        _code_block,
        text,
        flags=re.DOTALL,
    )

    # Inline code: `code`
    text = re.sub(
        r"`([^`]+)`",
        r'<code class="bg-surfaceLight px-1.5 py-0.5 rounded text-xs text-purple font-mono">\1</code>',
        text,
    )

    # Bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    # Italic: *text*
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # Links: [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\)]+)\)",
        r'<a href="\2" class="text-purple hover:text-gold underline transition" '
        r'target="_blank" rel="noopener">\1</a>',
        text,
    )

    # Auto-link bare URLs (not already inside href)
    text = re.sub(
        r'(?<!href=")(https?://\S+)',
        r'<a href="\1" class="text-purple hover:text-gold underline transition" '
        r'target="_blank" rel="noopener">\1</a>',
        text,
    )

    # Paragraphs: double newline → <p>
    paragraphs = re.split(r"\n{2,}", text.strip())
    text = "".join(
        f"<p>{p.strip()}</p>" if not p.strip().startswith("<pre") else p
        for p in paragraphs
        if p.strip()
    )

    # Single newlines inside paragraphs → <br>
    text = re.sub(r"(?<!</p>)\n(?!<)", "<br>", text)

    return text


@register.filter(name="render_markdown")
def render_markdown(value):
    """Render a markdown string to safe HTML."""
    if not value:
        return ""
    return mark_safe(_md_to_html(str(value)))


@register.filter(name="multiply")
def multiply(value, arg):
    """Multiply value by arg — used for indentation.

    Usage: {{ depth|multiply:24 }}  →  "48" (for 2 levels × 24px)
    """
    try:
        return int(value) * int(arg)
    except (ValueError, TypeError):
        return 0
