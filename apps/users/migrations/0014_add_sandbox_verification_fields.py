# Generated migration for Docker sandbox verification fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0013_add_hf_inference_endpoint_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="usergamemodel",
            name="verification_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("suspicious", "Suspicious"),
                ],
                default="pending",
                help_text="Result of the Docker sandbox security scan.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="usergamemodel",
            name="last_verified_commit",
            field=models.CharField(
                blank=True,
                help_text="Commit SHA that was last verified in the sandbox.",
                max_length=40,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="usergamemodel",
            name="last_verified_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Timestamp of the most recent sandbox verification.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="usergamemodel",
            name="scan_report",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="JSON output from the security scanner (bandit, modelscan, etc.).",
            ),
        ),
    ]
