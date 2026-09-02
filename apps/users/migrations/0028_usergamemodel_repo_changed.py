from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0027_add_paypal_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="usergamemodel",
            name="repo_changed",
            field=models.BooleanField(
                default=False,
                help_text="Set when this user changes their HF repo. Requires 30 rated games to clear.",
            ),
        ),
    ]
