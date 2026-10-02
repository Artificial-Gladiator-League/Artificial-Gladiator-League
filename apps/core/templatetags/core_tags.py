from django import template
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def profile_flag(user):
    """Flag <img> for the PROFILE PAGE only (never used in standings, leaderboards, etc.).

    Renders nothing unless the user has a country and ``show_flag`` is on. The images
    are the GIFs bundled with django-countries; a missing file renders nothing.
    """
    from apps.users.countries import country_name as _name, flag_static_path, is_valid_country

    code = getattr(user, "country", "") or ""
    if not code or not getattr(user, "show_flag", False) or not is_valid_country(code):
        return ""
    try:
        src = static(flag_static_path(code))
    except ValueError:
        return ""
    name = _name(code)
    return format_html('<img class="lp-flag" src="{}" alt="{}" title="{}" loading="lazy">', src, name, name)


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
