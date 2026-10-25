from django.contrib.sites.shortcuts import get_current_site
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

# Real paths from the URLconf. Repeated in every user-agent group because a crawler
# that matches a specific group ignores the "*" group entirely.
DISALLOWED_PATHS = [
    "/admin/",
    "/users/",
    "/games/",
    "/ws/",
    "/api/",
    "/media/",
    "/forum/post/",
    "/forum/c/*/new/",
    "/forum/c/*/t/*/reply/",
    "/blog/new/",
    "/blog/*/edit/",
    "/blog/*/delete/",
    "/tournaments/*/join/",
    "/tournaments/*/leave/",
    "/tournaments/*/ready/",
    "/tournaments/*/match/",
    "/tournaments/*/claim/",
    "/tournaments/*/payout-confirm/",
]

AI_CRAWLERS = [
    "GPTBot",
    "ClaudeBot",
    "PerplexityBot",
    "Google-Extended",
    "OAI-SearchBot",
]


@require_GET
@cache_control(max_age=3600, public=True)
def robots_txt(request):
    sitemap_url = f"https://{get_current_site(request).domain}/sitemap.xml"
    return render(
        request,
        "robots.txt",
        {
            "disallowed_paths": DISALLOWED_PATHS,
            "ai_crawlers": AI_CRAWLERS,
            "sitemap_url": sitemap_url,
        },
        content_type="text/plain; charset=utf-8",
    )
