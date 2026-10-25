"""/robots.txt and /sitemap.xml."""
from xml.etree import ElementTree

from django.test import TestCase
from django.urls import reverse

from apps.forum.models import Category, Topic
from apps.news.models import NewsPost

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
BASE = "https://agladiator.com"


class RobotsTxtTests(TestCase):
    def setUp(self):
        self.response = self.client.get("/robots.txt")
        self.body = self.response.content.decode()

    def test_served_as_plain_text(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertEqual(self.response["Content-Type"].split(";")[0], "text/plain")

    def test_default_group_and_sitemap(self):
        lines = self.body.splitlines()
        self.assertEqual(lines[:2], ["User-agent: *", "Allow: /"])
        for path in ("/admin/", "/users/", "/games/", "/ws/"):
            self.assertIn(f"Disallow: {path}", lines)
        self.assertIn(f"Sitemap: {BASE}/sitemap.xml", lines)

    def test_public_sections_are_not_blocked(self):
        for path in ("/how-to-upload/", "/how-it-works/", "/blog/", "/forum/",
                     "/tournaments/", "/leaderboard/", "/about/"):
            self.assertNotIn(f"Disallow: {path}", self.body.splitlines())

    def test_ai_crawlers_have_explicit_groups_that_keep_the_disallows(self):
        for bot in ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "OAI-SearchBot"):
            group = self.body.split(f"User-agent: {bot}\n", 1)[1].split("\n\n", 1)[0]
            self.assertTrue(group.startswith("Allow: /\n"), bot)
            self.assertIn("Disallow: /admin/", group)


class SitemapTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.post = NewsPost.objects.create(title="Hello", body="b", status=NewsPost.Status.PUBLISHED)
        cls.draft = NewsPost.objects.create(title="Secret draft", body="b")
        cls.category = Category.objects.create(name="Sitemap Cat", slug="sitemap-cat")
        cls.topic = Topic.objects.create(category=cls.category, title="Visible", body="b")
        cls.deleted = Topic.objects.create(category=cls.category, title="Gone", body="b", is_deleted=True)

    def _locs(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertIn("xml", response["Content-Type"])
        root = ElementTree.fromstring(response.content)  # raises if not well-formed
        self.assertTrue(root.tag.endswith("urlset"))
        return [el.text for el in root.findall("sm:url/sm:loc", NS)]

    def test_every_url_is_canonical_https(self):
        locs = self._locs()
        self.assertTrue(locs)
        for loc in locs:
            self.assertTrue(loc.startswith(f"{BASE}/"), loc)

    def test_static_pages_present(self):
        locs = self._locs()
        for name in ("core:home", "core:how_it_works", "core:how_to_upload", "core:about",
                     "core:contact", "core:leaderboard", "core:privacy", "core:terms",
                     "core:cookies", "core:accessibility", "tournaments:list",
                     "news:list", "forum:category_list"):
            self.assertIn(BASE + reverse(name), locs, name)

    def test_home_has_top_priority(self):
        root = ElementTree.fromstring(self.client.get("/sitemap.xml").content)
        priorities = {
            u.find("sm:loc", NS).text: u.find("sm:priority", NS).text
            for u in root.findall("sm:url", NS)
        }
        self.assertEqual(priorities[BASE + "/"], "1.0")
        self.assertEqual(priorities[BASE + reverse("core:how_it_works")], "0.9")
        self.assertEqual(priorities[BASE + reverse("core:how_to_upload")], "0.9")

    def test_only_public_dynamic_content(self):
        locs = self._locs()
        self.assertIn(BASE + self.post.get_absolute_url(), locs)
        self.assertNotIn(BASE + self.draft.get_absolute_url(), locs)
        self.assertIn(BASE + self.category.get_absolute_url(), locs)
        self.assertIn(BASE + self.topic.get_absolute_url(), locs)
        self.assertNotIn(BASE + self.deleted.get_absolute_url(), locs)

    def test_no_private_or_per_user_pages(self):
        for loc in self._locs():
            path = loc[len(BASE):]
            for prefix in ("/users/", "/games/", "/admin/", "/ws/", "/api/"):
                self.assertFalse(path.startswith(prefix), loc)
