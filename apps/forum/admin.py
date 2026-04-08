from django.contrib import admin

from .models import Category, Post, Thread


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("icon", "name", "slug", "ordering", "thread_count", "post_count")
    list_editable = ("ordering",)
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)
    ordering = ("ordering", "name")

    @admin.display(description="Threads")
    def thread_count(self, obj):
        return obj.threads.count()

    @admin.display(description="Posts")
    def post_count(self, obj):
        return Post.objects.filter(thread__category=obj).count()


class PostInline(admin.TabularInline):
    model = Post
    extra = 0
    fields = ("author", "parent", "body", "created_at", "is_deleted")
    readonly_fields = ("created_at",)
    show_change_link = True


@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = (
        "title", "category", "author", "created_at",
        "views_count", "reply_count_display",
        "is_pinned", "is_locked", "is_deleted",
    )
    list_filter = ("category", "is_pinned", "is_locked", "is_deleted")
    list_editable = ("is_pinned", "is_locked", "is_deleted")
    search_fields = ("title", "author__username")
    raw_id_fields = ("author", "linked_game", "linked_tournament")
    date_hierarchy = "created_at"
    inlines = [PostInline]
    readonly_fields = ("views_count", "last_activity")

    @admin.display(description="Replies")
    def reply_count_display(self, obj):
        return obj.posts.count()


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("__str__", "thread", "author", "parent", "created_at", "is_deleted")
    list_filter = ("is_deleted", "thread__category")
    search_fields = ("body", "author__username", "thread__title")
    raw_id_fields = ("author", "thread", "parent")
    date_hierarchy = "created_at"
