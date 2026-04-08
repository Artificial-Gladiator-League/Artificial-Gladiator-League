from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Category(models.Model):
    """Forum category — organises threads into discussion topics."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    icon = models.CharField(
        max_length=10,
        blank=True,
        help_text="Emoji icon displayed next to the category name.",
    )
    ordering = models.PositiveIntegerField(
        default=0,
        help_text="Lower numbers appear first.",
    )

    class Meta:
        ordering = ["ordering", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("forum:category", kwargs={"slug": self.slug})

    @property
    def thread_count(self):
        return self.threads.count()

    @property
    def post_count(self):
        return Post.objects.filter(thread__category=self).count()

    @property
    def latest_thread(self):
        return self.threads.order_by("-last_activity").first()


class Thread(models.Model):
    """A discussion thread inside a forum category."""

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="forum_threads",
    )
    title = models.CharField(max_length=200)
    body = models.TextField(
        help_text="Markdown supported: **bold**, *italic*, `code`, [links](url).",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_activity = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Bumped whenever a new reply is posted.",
    )
    views_count = models.PositiveIntegerField(default=0)
    is_pinned = models.BooleanField(default=False)
    is_locked = models.BooleanField(
        default=False,
        help_text="Locked threads cannot receive new replies.",
    )
    is_deleted = models.BooleanField(default=False)

    # Optional: link to a specific game replay or tournament
    linked_game = models.ForeignKey(
        "games.Game",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Optionally link a replay to this thread.",
    )
    linked_tournament = models.ForeignKey(
        "tournaments.Tournament",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Optionally link a tournament to this thread.",
    )

    class Meta:
        ordering = ["-is_pinned", "-last_activity"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("forum:thread", kwargs={"pk": self.pk})

    @property
    def reply_count(self):
        return self.posts.count()


class Post(models.Model):
    """A single reply inside a thread (supports nesting via parent FK)."""

    thread = models.ForeignKey(
        Thread,
        on_delete=models.CASCADE,
        related_name="posts",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="forum_posts",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        help_text="Leave empty for a top-level reply; set to nest under another post.",
    )
    body = models.TextField(
        help_text="Markdown supported.",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        author_name = self.author.username if self.author else "[deleted]"
        return f"Post by {author_name} on {self.thread.title}"

    @property
    def depth(self):
        """How many levels deep this reply is (0 = top-level reply)."""
        d = 0
        p = self.parent
        while p is not None:
            d += 1
            p = p.parent
        return d
