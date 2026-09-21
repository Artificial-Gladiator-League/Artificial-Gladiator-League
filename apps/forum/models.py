from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Category(models.Model):
    """Forum category — organises topics into discussion areas."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=110, unique=True)
    description = models.CharField(max_length=255, blank=True)
    icon = models.CharField(
        max_length=8,
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
        return reverse("forum:topic_list", kwargs={"category_slug": self.slug})

    @property
    def topic_count(self):
        return self.topics.filter(is_deleted=False).count()

    @property
    def post_count(self):
        return Post.objects.filter(topic__category=self, is_deleted=False).count()

    @property
    def latest_topic(self):
        return self.topics.filter(is_deleted=False).order_by("-last_activity").first()


class Topic(models.Model):
    """A discussion topic inside a forum category (the "first post" is its body)."""

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="topics",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="forum_topics",
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
        help_text="Locked topics cannot receive new replies.",
    )
    is_deleted = models.BooleanField(
        default=False,
        help_text="Soft-deleted topics are hidden but retained for moderation.",
    )

    class Meta:
        ordering = ["-is_pinned", "-last_activity"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse(
            "forum:topic_detail",
            kwargs={"category_slug": self.category.slug, "pk": self.pk},
        )

    @property
    def reply_count(self):
        return self.posts.filter(is_deleted=False).count()

    @property
    def is_new(self):
        return (timezone.now() - self.created_at).total_seconds() < 24 * 3600

    @property
    def is_hot(self):
        recent_cutoff = timezone.now() - timezone.timedelta(hours=48)
        return self.last_activity >= recent_cutoff and self.reply_count >= 5


class Post(models.Model):
    """A single reply inside a topic (supports nesting via parent FK)."""

    topic = models.ForeignKey(
        Topic,
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
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
        help_text="Leave empty for a top-level reply; set to nest under another post.",
    )
    body = models.TextField(
        help_text="Markdown supported: **bold**, *italic*, `code`, [links](url).",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        if self.is_deleted:
            return "[deleted]"
        author_name = self.author.username if self.author_id else "deleted user"
        return f"Post by {author_name} on {self.topic.title}"

    @property
    def depth(self):
        """How many levels deep this reply is (0 = top-level reply)."""
        depth = 0
        node = self.parent
        while node is not None and depth < 10:
            depth += 1
            node = node.parent
        return depth

    @property
    def like_count(self):
        return self.likes.count()


class PostLike(models.Model):
    """A like/upvote from a user on a single post."""

    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="likes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_likes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("post", "user"),)

    def __str__(self):
        return f"{self.user.username} likes post #{self.post_id}"


class TopicReadState(models.Model):
    """Tracks the last time a user read a topic, powering the "unread" topic list."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="forum_read_states",
    )
    topic = models.ForeignKey(
        Topic,
        on_delete=models.CASCADE,
        related_name="read_states",
    )
    last_read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = (("user", "topic"),)

    def __str__(self):
        return f"{self.user.username} read Topic #{self.topic_id} @ {self.last_read_at}"
