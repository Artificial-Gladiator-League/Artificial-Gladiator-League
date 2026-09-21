from django import forms

from .models import Post, Topic


class TopicForm(forms.ModelForm):
    """Form for creating a new discussion topic."""

    class Meta:
        model = Topic
        fields = ["title", "body"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "w-full rounded-lg border border-gray-300 dark:border-borderDark bg-white dark:bg-surface px-4 py-2.5 text-sm focus:ring-2 focus:ring-purple focus:border-transparent placeholder-gray-400",
                "placeholder": "Topic title…",
                "maxlength": "200",
            }),
            "body": forms.Textarea(attrs={
                "class": "w-full rounded-lg border border-gray-300 dark:border-borderDark bg-white dark:bg-surface px-4 py-3 text-sm font-mono focus:ring-2 focus:ring-purple focus:border-transparent placeholder-gray-400",
                "rows": 8,
                "placeholder": "Write your post here… Markdown supported:\n**bold**, *italic*, `code`, [links](url)",
            }),
        }


class PostForm(forms.ModelForm):
    """Form for replying to a topic or to another post."""

    class Meta:
        model = Post
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(attrs={
                "class": "w-full rounded-lg border border-gray-300 dark:border-borderDark bg-white dark:bg-surface px-4 py-3 text-sm font-mono focus:ring-2 focus:ring-purple focus:border-transparent placeholder-gray-400",
                "rows": 4,
                "placeholder": "Write a reply… Markdown supported.",
            }),
        }
