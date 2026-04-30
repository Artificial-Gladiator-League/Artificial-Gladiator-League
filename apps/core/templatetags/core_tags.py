from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def dictget(d, key):
    """Look up a dictionary value by key: {{ mydict|dictget:variable }}"""
    if isinstance(d, dict):
        return d.get(key, "")
    return ""


@register.filter
def subtract(value, arg):
    """Subtract arg from value: {{ 30|subtract:played }}"""
    try:
        return int(value) - int(arg)
    except (TypeError, ValueError):
        return 0


@register.filter
def country_flag(code):
    """Return an <img> tag for the country SVG flag.

    Usage:  {{ "IL"|country_flag }}  →  <img src="/static/flags/4x3/il.svg" …>
    Returns empty string for blank / invalid codes.
    """
    if not code or not isinstance(code, str) or len(code) != 2 or not code.isalpha():
        return ""
    lc = code.lower()
    alt = code.upper()
    return mark_safe(
        f'<img src="/static/flags/4x3/{lc}.svg" alt="{alt}" '
        f'class="inline-block h-5 w-auto align-middle" loading="lazy">'
    )


# Lazy-built lookup from COUNTRY_CHOICES code → human name.
_COUNTRY_NAMES: dict[str, str] | None = None


def _get_country_names() -> dict[str, str]:
    global _COUNTRY_NAMES
    if _COUNTRY_NAMES is None:
        from apps.users.forms import COUNTRY_CHOICES
        _COUNTRY_NAMES = {}
        for code, label in COUNTRY_CHOICES:
            if code:
                # label format: "🇮🇱 Israel" → extract name after flag+space
                parts = label.split(" ", 1)
                _COUNTRY_NAMES[code] = parts[1] if len(parts) > 1 else label
    return _COUNTRY_NAMES


@register.filter
def country_name(code):
    """Return the human-readable country name for an ISO alpha-2 code.

    Usage:  {{ user.country|country_name }}  →  "Israel"
    Falls back to the raw code if not found.
    """
    if not code or not isinstance(code, str):
        return ""
    return _get_country_names().get(code.upper(), code)


@register.filter
def gamemodel(user, game_type):
    """Return the UserGameModel for a user + game_type, or None.

    Usage:  {% with gm=user|gamemodel:tournament.game_type %}
    """
    if not user or not hasattr(user, 'pk'):
        return None
    from apps.users.models import UserGameModel
    try:
        return UserGameModel.objects.get(user=user, game_type=game_type)
    except UserGameModel.DoesNotExist:
        return None


@register.simple_tag
def fide_badge(user):
    """Render a FIDE title badge for a user object.

    Usage:  {% fide_badge user_object %}
    Returns empty string if user has no title (ELO < 1200).
    """
    if not user or not hasattr(user, 'get_fide_title'):
        return ''
    fide = user.get_fide_title()
    abbr = fide.get('abbr', '')
    if not abbr:
        return ''
    css = fide.get('css', '')
    title = fide.get('title', '')
    return mark_safe(
        f'<span class="{css} font-bold text-xs" title="{title}">{abbr}</span>'
    )
