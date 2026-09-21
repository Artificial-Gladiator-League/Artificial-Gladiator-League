import logging
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import PostForm, TopicForm
from .models import Category, Post, PostLike, Topic, TopicReadState

log = logging.getLogger(__name__)

TOPICS_PER_PAGE = 20
POSTS_PER_PAGE = 20


def category_list(request):
    """Forum homepage — all categories with topic/post counts and latest activity."""
    categories = Category.objects.annotate(
        num_topics=Count("topics", filter=Q(topics__is_deleted=False), distinct=True),
        num_posts=Count(
            "topics__posts",
            filter=Q(topics__is_deleted=False, topics__posts__is_deleted=False),
            distinct=True,
        ),
        last_topic_activity=Max("topics__last_activity", filter=Q(topics__is_deleted=False)),
    ).order_by("ordering", "name")

    latest_topics = (
        Topic.objects.filter(is_deleted=False)
        .select_related("author", "category")
        .order_by("-last_activity")[:10]
    )

    return render(request, "forum/home.html", {
        "categories": categories,
        "latest_topics": latest_topics,
        "total_topics": Topic.objects.filter(is_deleted=False).count(),
        "total_posts": Post.objects.filter(is_deleted=False).count(),
    })


def topic_list(request, category_slug):
    """Paginated, sortable (latest / top / unread) list of topics in a category."""
    category = get_object_or_404(Category, slug=category_slug)
    sort = request.GET.get("sort", "latest")

    topics = (
        category.topics.filter(is_deleted=False)
        .select_related("author")
        .annotate(num_replies=Count("posts", filter=Q(posts__is_deleted=False)))
    )

    if sort == "top":
        topics = topics.order_by("-is_pinned", "-num_replies", "-last_activity")
    elif sort == "unread" and request.user.is_authenticated:
        read_at_sq = TopicReadState.objects.filter(
            user=request.user, topic=OuterRef("pk"),
        ).values("last_read_at")[:1]
        topics = topics.annotate(_read_at=Subquery(read_at_sq)).filter(
            Q(_read_at__isnull=True) | Q(last_activity__gt=F("_read_at"))
        ).order_by("-is_pinned", "-last_activity")
    else:
        sort = "latest"
        topics = topics.order_by("-is_pinned", "-last_activity")

    paginator = Paginator(topics, TOPICS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "forum/category.html", {
        "category": category,
        "page_obj": page_obj,
        "sort": sort,
    })


def topic_detail(request, category_slug, pk):
    """Show a topic with all its posts in threaded (nested) order."""
    topic = get_object_or_404(
        Topic.objects.select_related("author", "category"),
        pk=pk, category__slug=category_slug, is_deleted=False,
    )

    Topic.objects.filter(pk=topic.pk).update(views_count=F("views_count") + 1)

    if request.user.is_authenticated:
        TopicReadState.objects.update_or_create(
            user=request.user, topic=topic,
            defaults={"last_read_at": timezone.now()},
        )

    posts = list(
        topic.posts.filter(is_deleted=False)
        .select_related("author")
        .order_by("created_at")
    )

    # Flatten the parent/child tree into reply order, tracking indent depth (capped at 4).
    children_map = defaultdict(list)
    roots = []
    posts_by_id = {p.pk: p for p in posts}
    for post in posts:
        if post.parent_id and post.parent_id in posts_by_id:
            children_map[post.parent_id].append(post)
        else:
            roots.append(post)

    ordered = []

    def _walk(node, depth):
        node.indent = min(depth, 4)
        ordered.append(node)
        for child in children_map.get(node.pk, []):
            _walk(child, depth + 1)

    for root in roots:
        _walk(root, 0)

    if request.user.is_authenticated:
        liked_ids = set(
            PostLike.objects.filter(user=request.user, post__topic=topic)
            .values_list("post_id", flat=True)
        )
        for post in ordered:
            post.user_has_liked = post.pk in liked_ids

    paginator = Paginator(ordered, POSTS_PER_PAGE)
    posts_page = paginator.get_page(request.GET.get("page"))

    return render(request, "forum/topic.html", {
        "topic": topic,
        "posts_page": posts_page,
        "reply_form": PostForm(),
        "total_replies": len(posts),
    })


@login_required
def new_topic(request, category_slug):
    """Form to create a new topic in the given category."""
    category = get_object_or_404(Category, slug=category_slug)

    if request.method == "POST":
        form = TopicForm(request.POST)
        if form.is_valid():
            topic = form.save(commit=False)
            topic.category = category
            topic.author = request.user
            topic.save()
            messages.success(request, "Topic created!")
            return redirect(topic.get_absolute_url())
    else:
        form = TopicForm()

    return render(request, "forum/new_topic.html", {"category": category, "form": form})


@login_required
@require_POST
def add_reply(request, category_slug, pk):
    """Post a reply to a topic (or nested under an existing post)."""
    topic = get_object_or_404(Topic, pk=pk, category__slug=category_slug, is_deleted=False)

    if topic.is_locked:
        messages.error(request, "This topic is locked — no new replies allowed.")
        return redirect(topic.get_absolute_url())

    form = PostForm(request.POST)
    if form.is_valid():
        post = form.save(commit=False)
        post.topic = topic
        post.author = request.user

        parent_id = request.POST.get("parent_id")
        if parent_id:
            try:
                post.parent = Post.objects.get(pk=int(parent_id), topic=topic)
            except (Post.DoesNotExist, ValueError):
                pass

        post.save()
        Topic.objects.filter(pk=topic.pk).update(last_activity=timezone.now())
        messages.success(request, "Reply posted!")
        return redirect(f"{topic.get_absolute_url()}#post-{post.pk}")

    messages.error(request, "Reply could not be posted — please check your input.")
    return redirect(topic.get_absolute_url())


@login_required
@require_POST
def toggle_like(request, pk):
    """Toggle the current user's like on a post."""
    post = get_object_or_404(Post, pk=pk, is_deleted=False)
    like, created = PostLike.objects.get_or_create(post=post, user=request.user)
    if not created:
        like.delete()
    return redirect(f"{post.topic.get_absolute_url()}#post-{post.pk}")
