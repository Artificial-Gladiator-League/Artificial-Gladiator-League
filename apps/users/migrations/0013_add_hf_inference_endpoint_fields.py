# Generated migration for HF Inference Endpoint fields on UserGameModel.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0012_add_hf_data_repo_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="usergamemodel",
            name="hf_inference_endpoint_url",
            field=models.URLField(
                blank=True,
                help_text="URL of the HF Inference Endpoint for this model.",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="usergamemodel",
            name="hf_inference_endpoint_name",
            field=models.CharField(
                blank=True,
                help_text="HF Inference Endpoint name (e.g. 'agl-42-chess').",
                max_length=120,
            ),
        ),
    ]
