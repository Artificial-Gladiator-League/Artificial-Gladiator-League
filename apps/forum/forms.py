from django import forms

from .models import Post, Thread


class ThreadForm(forms.ModelForm):
    """Form for creating a new discussion thread."""

    class Meta:
        model = Thread
        fields = ["title", "body", "linked_game", "linked_tournament"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": (
                    "w-full rounded-lg border border-gray-300 dark:border-borderDark "
                    "bg-white dark:bg-surface px-4 py-2.5 text-sm "
                    "focus:ring-2 focus:ring-purple focus:border-transparent "
                    "placeholder-gray-400"
                ),
                "placeholder": "Thread title…",
                "maxlength": "200",
            }),
            "body": forms.Textarea(attrs={
                "class": (
                    "w-full rounded-lg border border-gray-300 dark:border-borderDark "
                    "bg-white dark:bg-surface px-4 py-3 text-sm font-mono "
                    "focus:ring-2 focus:ring-purple focus:border-transparent "
                    "placeholder-gray-400"
                ),
                "rows": 10,
                "placeholder": (
                    "Write your post here… Markdown supported:\n"
                    "**bold**, *italic*, `code`, ```code blocks```, [links](url)"
                ),
            }),
            "linked_game": forms.NumberInput(attrs={
                "class": (
                    "w-full rounded-lg border border-gray-300 dark:border-borderDark "
                    "bg-white dark:bg-surface px-4 py-2 text-sm "
                    "focus:ring-2 focus:ring-purple focus:border-transparent "
                    "placeholder-gray-400"
                ),
                "placeholder": "Game ID (optional)",
            }),
            "linked_tournament": forms.Select(attrs={
                "class": (
                    "w-full rounded-lg border border-gray-300 dark:border-borderDark "
                    "bg-white dark:bg-surface px-4 py-2 text-sm "
                    "focus:ring-2 focus:ring-purple focus:border-transparent"
                ),
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["linked_game"].required = False
        self.fields["linked_tournament"].required = False
        self.fields["linked_tournament"].empty_label = "— Link a tournament (optional)"


class PostForm(forms.ModelForm):
    """Form for replying to a thread or to another post."""

    class Meta:
        model = Post
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(attrs={
                "class": (
                    "w-full rounded-lg border border-gray-300 dark:border-borderDark "
                    "bg-white dark:bg-surface px-4 py-3 text-sm font-mono "
                    "focus:ring-2 focus:ring-purple focus:border-transparent "
                    "placeholder-gray-400"
                ),
                "rows": 5,
                "placeholder": "Write a reply… Markdown supported.",
            }),
        }
