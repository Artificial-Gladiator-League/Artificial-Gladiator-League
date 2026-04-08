from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import PostForm, ThreadForm
from .models import Category, Post, Thread


THREADS_PER_PAGE = 20
POSTS_PER_PAGE = 30


# ─── Forum Home ──────────────────────────────────
def forum_home(request):
    """Show all categories with thread / post counts and latest activity."""
    categories = (
        Category.objects
        .annotate(
            num_threads=Count("threads", distinct=True),
            num_posts=Count("threads__posts", distinct=True),
            last_thread_activity=Max("threads__last_activity"),
        )
        .order_by("ordering", "name")
    )

    # Site-wide latest threads (for a sidebar or "recent" section)
    latest_threads = (
        Thread.objects
        .filter(is_deleted=False)
        .select_related("author", "category")
        .order_by("-last_activity")[:5]
    )

    # Stats
    total_threads = Thread.objects.filter(is_deleted=False).count()
    total_posts = Post.objects.filter(is_deleted=False).count()

    return render(request, "forum/home.html", {
        "categories": categories,
        "latest_threads": latest_threads,
        "total_threads": total_threads,
        "total_posts": total_posts,
    })


# ─── Category Detail ─────────────────────────────
def category_detail(request, slug):
    """Paginated list of threads in a single category."""
    category = get_object_or_404(Category, slug=slug)
    threads_qs = (
        category.threads
        .filter(is_deleted=False)
        .select_related("author")
        .annotate(num_replies=Count("posts"))
        .order_by("-is_pinned", "-last_activity")
    )

    paginator = Paginator(threads_qs, THREADS_PER_PAGE)
    page = request.GET.get("page")
    threads = paginator.get_page(page)

    return render(request, "forum/category.html", {
        "category": category,
        "threads": threads,
    })


# ─── Thread Detail ───────────────────────────────
def thread_detail(request, pk):
    """Show a thread with all its posts in threaded (nested) order."""
    thread = get_object_or_404(
        Thread.objects.select_related("author", "category", "linked_game", "linked_tournament"),
        pk=pk,
        is_deleted=False,
    )

    # Bump view count
    Thread.objects.filter(pk=pk).update(views_count=F("views_count") + 1)

    # Fetch all posts eagerly, then build the tree in Python
    all_posts = list(
        thread.posts
        .filter(is_deleted=False)
        .select_related("author", "parent")
        .order_by("created_at")
    )

    # Build nested tree structure
    post_map = {p.pk: p for p in all_posts}
    for p in all_posts:
        p._children = []
    roots = []
    for p in all_posts:
        if p.parent_id and p.parent_id in post_map:
            post_map[p.parent_id]._children.append(p)
        else:
            roots.append(p)

    # Flatten tree into DFS order with depth info
    flat_posts = []

    def _walk(node, depth=0):
        node.indent = min(depth, 5)  # Cap visual nesting at 5 levels
        flat_posts.append(node)
        for child in node._children:
            _walk(child, depth + 1)

    for root in roots:
        _walk(root)

    # Paginate the flattened post list
    paginator = Paginator(flat_posts, POSTS_PER_PAGE)
    page = request.GET.get("page")
    posts_page = paginator.get_page(page)

    # Reply form
    reply_form = PostForm()

    return render(request, "forum/thread.html", {
        "thread": thread,
        "posts_page": posts_page,
        "reply_form": reply_form,
        "total_replies": len(all_posts),
    })


# ─── Create Thread ───────────────────────────────
@login_required
def create_thread(request, slug):
    """Form to create a new thread in the given category."""
    category = get_object_or_404(Category, slug=slug)

    if request.method == "POST":
        form = ThreadForm(request.POST)
        if form.is_valid():
            thread = form.save(commit=False)
            thread.category = category
            thread.author = request.user
            thread.save()
            messages.success(request, "Thread created!")
            return redirect(thread.get_absolute_url())
    else:
        form = ThreadForm()

    return render(request, "forum/create_thread.html", {
        "category": category,
        "form": form,
    })


# ─── Reply to Thread / Post ─────────────────────
@login_required
def reply_to_thread(request, pk):
    """Post a reply to a thread (or nested under an existing post)."""
    thread = get_object_or_404(Thread, pk=pk, is_deleted=False)

    if thread.is_locked:
        messages.error(request, "This thread is locked — no new replies allowed.")
        return redirect(thread.get_absolute_url())

    if request.method == "POST":
        form = PostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.thread = thread
            post.author = request.user

            # Check for parent (nested reply)
            parent_id = request.POST.get("parent")
            if parent_id:
                try:
                    parent_post = Post.objects.get(
                        pk=int(parent_id), thread=thread, is_deleted=False,
                    )
                    post.parent = parent_post
                except (Post.DoesNotExist, ValueError):
                    pass

            post.save()

            # Bump thread's last_activity
            thread.last_activity = timezone.now()
            thread.save(update_fields=["last_activity"])

            messages.success(request, "Reply posted!")
            return redirect(thread.get_absolute_url() + f"#post-{post.pk}")
    else:
        form = PostForm()

    return render(request, "forum/reply.html", {
        "thread": thread,
        "form": form,
        "parent_id": request.GET.get("parent", ""),
    })
