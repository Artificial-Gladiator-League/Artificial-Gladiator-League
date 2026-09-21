from django import forms

from .models import NewsPost

TEXT_INPUT_CLASSES = (
    "w-full rounded-lg border border-gray-300 dark:border-borderDark bg-white dark:bg-surface "
    "px-4 py-2.5 text-sm focus:ring-2 focus:ring-purple focus:border-transparent placeholder-gray-400"
)
TEXTAREA_CLASSES = (
    "w-full rounded-lg border border-gray-300 dark:border-borderDark bg-white dark:bg-surface "
    "px-4 py-3 text-sm font-mono focus:ring-2 focus:ring-purple focus:border-transparent placeholder-gray-400"
)


class NewsPostForm(forms.ModelForm):
    """Form used for both creating and editing a news post (staff only)."""

    class Meta:
        model = NewsPost
        fields = ["title", "summary", "body", "tags", "status", "is_pinned"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": TEXT_INPUT_CLASSES,
                "placeholder": "Post title…",
                "maxlength": "200",
            }),
            "summary": forms.Textarea(attrs={
                "class": TEXTAREA_CLASSES,
                "rows": 3,
                "placeholder": "Short preview (optional — auto-generated from the body if left blank)…",
            }),
            "body": forms.Textarea(attrs={
                "class": TEXTAREA_CLASSES,
                "rows": 12,
                "placeholder": "Write the announcement here… Markdown supported:\n**bold**, *italic*, `code`, [links](url)",
            }),
            "tags": forms.TextInput(attrs={
                "class": TEXT_INPUT_CLASSES,
                "placeholder": "comma, separated, tags",
            }),
            "status": forms.Select(attrs={"class": TEXT_INPUT_CLASSES}),
            "is_pinned": forms.CheckboxInput(attrs={
                "class": "rounded border-gray-300 dark:border-borderDark text-purple focus:ring-purple",
            }),
        }
