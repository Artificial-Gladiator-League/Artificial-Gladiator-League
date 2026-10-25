"""Sitemaps for /sitemap.xml — public, crawlable pages only (never per-user pages)."""
from django.contrib.sitemaps import Sitemap
from django.db.models import Max, Q
from django.urls import reverse

from apps.forum.models import Category, Topic
from apps.news.models import NewsPost


class _HttpsSitemap(Sitemap):
    # Domain comes from the Site row (agladiator.com); never from the request host.
    protocol = "https"


class StaticViewSitemap(_HttpsSitemap):
    # url name -> (priority, changefreq)
    PAGES = {
        "core:home": (1.0, "daily"),
        "core:how_it_works": (0.9, "monthly"),
        "core:how_to_upload": (0.9, "monthly"),
        "tournaments:list": (0.8, "daily"),
        "news:list": (0.8, "daily"),
        "forum:category_list": (0.8, "daily"),
        "core:leaderboard": (0.7, "daily"),
        "core:about": (0.6, "monthly"),
        "core:contact": (0.4, "yearly"),
        "core:privacy": (0.3, "yearly"),
        "core:terms": (0.3, "yearly"),
        "core:cookies": (0.3, "yearly"),
        "core:accessibility": (0.3, "yearly"),
    }

    def items(self):
        return list(self.PAGES)

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return self.PAGES[item][0]

    def changefreq(self, item):
        return self.PAGES[item][1]


class NewsPostSitemap(_HttpsSitemap):
    changefreq = "weekly"
    priority = 0.7

    def items(self):
        return NewsPost.objects.filter(status=NewsPost.Status.PUBLISHED).order_by("-published_at", "pk")

    def lastmod(self, obj):
        return obj.updated_at


class ForumCategorySitemap(_HttpsSitemap):
    changefreq = "daily"
    priority = 0.6

    def items(self):
        return Category.objects.annotate(
            last_activity=Max("topics__last_activity", filter=Q(topics__is_deleted=False)),
        ).order_by("ordering", "name")

    def lastmod(self, obj):
        return obj.last_activity


class ForumTopicSitemap(_HttpsSitemap):
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return Topic.objects.filter(is_deleted=False).select_related("category").order_by("pk")

    def lastmod(self, obj):
        return obj.last_activity


sitemaps = {
    "static": StaticViewSitemap,
    "news": NewsPostSitemap,
    "forum-categories": ForumCategorySitemap,
    "forum-topics": ForumTopicSitemap,
}
