import re
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import NewsPostForm
from .models import NewsPost

POSTS_PER_PAGE = 10


def staff_required(view_func):
    """Only staff users may access the wrapped view (403 for non-staff)."""
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped


def news_list(request):
    """Published news feed, paginated, with an optional ?tag= filter."""
    posts = (
        NewsPost.objects.filter(status=NewsPost.Status.PUBLISHED)
        .select_related("author")
    )

    tag = request.GET.get("tag", "").strip()
    if tag:
        posts = posts.filter(tags__iregex=rf"(^|,)\s*{re.escape(tag)}\s*(,|$)")

    paginator = Paginator(posts, POSTS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    pinned_posts = NewsPost.objects.filter(
        status=NewsPost.Status.PUBLISHED, is_pinned=True,
    ).select_related("author")[:3]

    recent_posts = NewsPost.objects.filter(
        status=NewsPost.Status.PUBLISHED,
    ).select_related("author")[:5]

    all_tags = set()
    for raw_tags in NewsPost.objects.filter(
        status=NewsPost.Status.PUBLISHED,
    ).values_list("tags", flat=True):
        all_tags.update(t.strip() for t in raw_tags.split(",") if t.strip())

    return render(request, "news/list.html", {
        "page_obj": page_obj,
        "tag": tag,
        "pinned_posts": pinned_posts,
        "recent_posts": recent_posts,
        "all_tags": sorted(all_tags),
    })


def news_detail(request, slug):
    """Full news post — drafts are visible to staff only (404 for everyone else)."""
    post = get_object_or_404(NewsPost.objects.select_related("author"), slug=slug)
    is_staff_viewer = request.user.is_authenticated and request.user.is_staff
    if post.status != NewsPost.Status.PUBLISHED and not is_staff_viewer:
        raise Http404("News post not found.")
    return render(request, "news/detail.html", {"post": post})


@staff_required
def news_create(request):
    """Staff-only form to create a new news post."""
    if request.method == "POST":
        form = NewsPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.author = request.user
            post.save()
            messages.success(request, "News post created.")
            return redirect(post.get_absolute_url())
    else:
        form = NewsPostForm()

    return render(request, "news/form.html", {"form": form, "mode": "create"})


@staff_required
def news_edit(request, slug):
    """Staff-only form to edit an existing news post."""
    post = get_object_or_404(NewsPost, slug=slug)

    if request.method == "POST":
        form = NewsPostForm(request.POST, instance=post)
        if form.is_valid():
            form.save()
            messages.success(request, "News post updated.")
            return redirect(post.get_absolute_url())
    else:
        form = NewsPostForm(instance=post)

    return render(request, "news/form.html", {"form": form, "mode": "edit", "post": post})


@staff_required
def news_delete(request, slug):
    """Staff-only delete confirmation (GET) and deletion (POST + CSRF)."""
    post = get_object_or_404(NewsPost, slug=slug)

    if request.method == "POST":
        post.delete()
        messages.success(request, "News post deleted.")
        return redirect("news:list")

    return render(request, "news/confirm_delete.html", {"post": post})
