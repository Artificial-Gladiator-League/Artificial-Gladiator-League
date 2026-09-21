from django.contrib import admin

from .models import Category, Post, PostLike, Topic, TopicReadState


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("icon", "name", "slug", "ordering", "topic_count", "post_count")
    list_editable = ("ordering",)
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)
    ordering = ("ordering", "name")

    @admin.display(description="Topics")
    def topic_count(self, obj):
        return obj.topics.count()

    @admin.display(description="Posts")
    def post_count(self, obj):
        return Post.objects.filter(topic__category=obj).count()


class PostInline(admin.TabularInline):
    model = Post
    extra = 0
    fields = ("author", "parent", "body", "created_at", "is_deleted")
    readonly_fields = ("created_at",)
    show_change_link = True


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = (
        "title", "category", "author", "created_at", "views_count",
        "reply_count_display", "is_pinned", "is_locked", "is_deleted",
    )
    list_filter = ("category", "is_pinned", "is_locked", "is_deleted")
    list_editable = ("is_pinned", "is_locked", "is_deleted")
    search_fields = ("title", "author__username")
    raw_id_fields = ("author",)
    date_hierarchy = "created_at"
    inlines = [PostInline]
    readonly_fields = ("views_count", "last_activity")
    actions = ["soft_delete", "restore"]

    @admin.display(description="Replies")
    def reply_count_display(self, obj):
        return obj.posts.count()

    @admin.action(description="Soft-delete selected topics (hide from public view)")
    def soft_delete(self, request, queryset):
        updated = queryset.update(is_deleted=True)
        self.message_user(request, f"Soft-deleted {updated} topic(s).")

    @admin.action(description="Restore selected topics")
    def restore(self, request, queryset):
        updated = queryset.update(is_deleted=False)
        self.message_user(request, f"Restored {updated} topic(s).")


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("__str__", "topic", "author", "parent", "created_at", "is_deleted")
    list_filter = ("is_deleted", "topic__category")
    search_fields = ("body", "author__username", "topic__title")
    raw_id_fields = ("author", "topic", "parent")
    date_hierarchy = "created_at"
    actions = ["soft_delete", "restore"]

    @admin.action(description="Soft-delete selected posts (hide from public view)")
    def soft_delete(self, request, queryset):
        updated = queryset.update(is_deleted=True)
        self.message_user(request, f"Soft-deleted {updated} post(s).")

    @admin.action(description="Restore selected posts")
    def restore(self, request, queryset):
        updated = queryset.update(is_deleted=False)
        self.message_user(request, f"Restored {updated} post(s).")


@admin.register(PostLike)
class PostLikeAdmin(admin.ModelAdmin):
    list_display = ("post", "user", "created_at")
    raw_id_fields = ("post", "user")
    search_fields = ("user__username",)


@admin.register(TopicReadState)
class TopicReadStateAdmin(admin.ModelAdmin):
    list_display = ("user", "topic", "last_read_at")
    raw_id_fields = ("user", "topic")
