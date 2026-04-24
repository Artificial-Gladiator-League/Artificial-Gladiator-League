from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0021_usergamemodel_local_path"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="usergamemodel",
            name="sandbox_verified",
        ),
        migrations.RemoveField(
            model_name="usergamemodel",
            name="local_path",
        ),
        migrations.RemoveField(
            model_name="usergamemodel",
            name="local_integrity_baseline",
        ),
    ]
