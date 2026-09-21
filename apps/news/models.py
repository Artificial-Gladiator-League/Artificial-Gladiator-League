from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import Truncator, slugify


class NewsPost(models.Model):
    """An admin-authored news/announcement post (Codeforces-style blog feed)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="news_posts",
    )
    summary = models.TextField(
        blank=True,
        help_text="Short preview shown in the feed. Auto-generated from the body if left blank.",
    )
    body = models.TextField(
        help_text="Markdown supported: **bold**, *italic*, `code`, [links](url).",
    )
    tags = models.CharField(
        max_length=255,
        blank=True,
        help_text="Comma-separated tags, e.g. \"tournaments, changelog\".",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT,
    )
    is_pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set automatically the first time this post is published.",
    )

    class Meta:
        ordering = ["-is_pinned", "-published_at"]
        indexes = [
            models.Index(fields=["status", "is_pinned", "-published_at"]),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("news:detail", kwargs={"slug": self.slug})

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title)[:190] or "post"
            slug = base_slug
            n = 2
            while NewsPost.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{n}"
                n += 1
            self.slug = slug

        if not self.summary and self.body:
            self.summary = Truncator(self.body).words(40, truncate=" …")

        if self.status == self.Status.PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()

        super().save(*args, **kwargs)
