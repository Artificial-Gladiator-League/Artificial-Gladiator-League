from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import NewsPost

User = get_user_model()


def _user(username, is_staff=False):
    return User.objects.create_user(
        username=username, email=f"{username}@e.com", password="x", is_staff=is_staff,
    )


def _post(title="Hello World", status=NewsPost.Status.PUBLISHED, **kw):
    defaults = dict(title=title, body="Body text here.", status=status)
    defaults.update(kw)
    return NewsPost.objects.create(**defaults)


class AccessControlTests(TestCase):
    def setUp(self):
        self.staff = _user("staffer", is_staff=True)
        self.regular = _user("regular")

    def test_anonymous_cannot_create(self):
        resp = self.client.get(reverse("news:create"))
        self.assertEqual(resp.status_code, 302)  # redirected to login

    def test_non_staff_cannot_create(self):
        self.client.force_login(self.regular)
        resp = self.client.get(reverse("news:create"))
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_cannot_edit(self):
        post = _post()
        self.client.force_login(self.regular)
        resp = self.client.get(reverse("news:edit", args=[post.slug]))
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_cannot_delete(self):
        post = _post()
        self.client.force_login(self.regular)
        resp = self.client.post(reverse("news:delete", args=[post.slug]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(NewsPost.objects.filter(pk=post.pk).exists())

    def test_staff_can_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("news:create"), {
            "title": "Staff Post", "summary": "", "body": "Some content.",
            "tags": "", "status": NewsPost.Status.PUBLISHED, "is_pinned": False,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(NewsPost.objects.filter(title="Staff Post").exists())

    def test_staff_can_delete(self):
        post = _post()
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("news:delete", args=[post.slug]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(NewsPost.objects.filter(pk=post.pk).exists())


class DraftVisibilityTests(TestCase):
    def setUp(self):
        self.staff = _user("staffer", is_staff=True)
        self.regular = _user("regular")
        self.draft = _post(title="Secret Draft", status=NewsPost.Status.DRAFT)
        self.published = _post(title="Public Post", status=NewsPost.Status.PUBLISHED)

    def test_draft_hidden_from_public_list(self):
        resp = self.client.get(reverse("news:list"))
        self.assertNotContains(resp, "Secret Draft")
        self.assertContains(resp, "Public Post")

    def test_draft_detail_404_for_anonymous(self):
        resp = self.client.get(reverse("news:detail", args=[self.draft.slug]))
        self.assertEqual(resp.status_code, 404)

    def test_draft_detail_404_for_non_staff(self):
        self.client.force_login(self.regular)
        resp = self.client.get(reverse("news:detail", args=[self.draft.slug]))
        self.assertEqual(resp.status_code, 404)

    def test_draft_detail_visible_to_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("news:detail", args=[self.draft.slug]))
        self.assertEqual(resp.status_code, 200)

    def test_published_detail_visible_to_everyone(self):
        resp = self.client.get(reverse("news:detail", args=[self.published.slug]))
        self.assertEqual(resp.status_code, 200)


class SlugUniquenessTests(TestCase):
    def test_duplicate_titles_get_unique_slugs(self):
        p1 = _post(title="Same Title")
        p2 = _post(title="Same Title")
        p3 = _post(title="Same Title")
        self.assertEqual(p1.slug, "same-title")
        self.assertNotEqual(p1.slug, p2.slug)
        self.assertNotEqual(p2.slug, p3.slug)

    def test_published_at_set_once(self):
        post = _post(status=NewsPost.Status.DRAFT)
        self.assertIsNone(post.published_at)
        post.status = NewsPost.Status.PUBLISHED
        post.save()
        first_published_at = post.published_at
        self.assertIsNotNone(first_published_at)

        post.title = "Updated title"
        post.save()
        self.assertEqual(post.published_at, first_published_at)


class PaginationTests(TestCase):
    def test_list_paginates_at_ten_per_page(self):
        for i in range(15):
            _post(title=f"Post {i}")

        resp = self.client.get(reverse("news:list"))
        self.assertEqual(len(resp.context["page_obj"]), 10)

        resp_page2 = self.client.get(reverse("news:list"), {"page": 2})
        self.assertEqual(len(resp_page2.context["page_obj"]), 5)
