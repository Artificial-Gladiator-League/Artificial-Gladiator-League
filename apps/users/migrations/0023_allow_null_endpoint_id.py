"""Migration 0023: Ensure hf_inference_endpoint_id allows NULL in MySQL.

The Django model already declares null=True/blank=True/default='', but the
live MySQL column may still have a NOT NULL constraint if it was created before
migration 0018 added null=True.  This migration re-applies AlterField to force
MySQL to ALTER the column to NULL-able.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0022_remove_sandbox_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="usergamemodel",
            name="hf_inference_endpoint_id",
            field=models.CharField(
                blank=True,
                null=True,
                default="",
                max_length=120,
                help_text=(
                    "HF Inference Endpoint ID (for compatibility with DB, can be blank)"
                ),
            ),
        ),
        migrations.AlterField(
            model_name="usergamemodel",
            name="hf_inference_endpoint_status",
            field=models.CharField(
                blank=True,
                null=True,
                default="",
                max_length=40,
                help_text="HF Inference Endpoint status (compatibility field, can be blank)",
            ),
        ),
    ]
