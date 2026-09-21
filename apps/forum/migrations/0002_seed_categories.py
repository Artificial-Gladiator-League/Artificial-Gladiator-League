from django.db import migrations

CATEGORIES = [
    {
        "name": "General",
        "slug": "general",
        "icon": "💬",
        "description": "General discussion about the platform, AI, and everything in between.",
        "ordering": 0,
    },
    {
        "name": "Feedback & Feature Requests",
        "slug": "feedback-feature-requests",
        "icon": "💡",
        "description": "Suggest new features and share feedback on how to improve the platform.",
        "ordering": 1,
    },
    {
        "name": "Developer Help",
        "slug": "developer-help",
        "icon": "🛠️",
        "description": "Stuck building or uploading your AI model? Ask the community for help.",
        "ordering": 2,
    },
    {
        "name": "Strategy & Match Analysis",
        "slug": "strategy-match-analysis",
        "icon": "♟️",
        "description": "Break down games, discuss openings, and analyze what makes a winning AI.",
        "ordering": 3,
    },
    {
        "name": "Tournaments & Challenges",
        "slug": "tournaments-challenges",
        "icon": "⚔️",
        "description": "Talk about upcoming, ongoing, and past tournaments and gauntlets.",
        "ordering": 4,
    },
]


def seed_categories(apps, schema_editor):
    Category = apps.get_model("forum", "Category")
    for cat in CATEGORIES:
        Category.objects.get_or_create(slug=cat["slug"], defaults=cat)


def unseed_categories(apps, schema_editor):
    Category = apps.get_model("forum", "Category")
    Category.objects.filter(slug__in=[c["slug"] for c in CATEGORIES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("forum", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_categories, reverse_code=unseed_categories),
    ]
