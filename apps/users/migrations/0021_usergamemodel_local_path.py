# Generated migration: add local_path and local_integrity_baseline to UserGameModel.
# These fields support the local-repo architecture where model files are
# git-committed instead of downloaded from Hugging Face at runtime.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0020_usergamemodel_sandbox_verified"),
    ]

    operations = [
        migrations.AddField(
            model_name="usergamemodel",
            name="local_path",
            field=models.CharField(
                blank=True,
                default="",
                max_length=1024,
                help_text=(
                    "Filesystem path to the git-committed model directory root "
                    "(e.g. /var/lib/agladiator/user_models/user_42/chess). "
                    "Set automatically at login. model/ and data/ sub-dirs are "
                    "resolved from this root."
                ),
            ),
        ),
        migrations.AddField(
            model_name="usergamemodel",
            name="local_integrity_baseline",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "JSON mapping of relative file paths → SHA-256 hex digests. "
                    "Populated by record_local_baseline(). Compared against live "
                    "files before tournament entry."
                ),
            ),
        ),
    ]
