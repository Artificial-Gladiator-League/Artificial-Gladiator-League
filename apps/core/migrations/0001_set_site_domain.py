from django.conf import settings
from django.db import migrations

DOMAIN = "agladiator.com"


def set_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    # Runs before sites' post_migrate creates its default example.com row, which is
    # skipped once a Site exists.
    Site.objects.update_or_create(
        pk=settings.SITE_ID,
        defaults={"domain": DOMAIN, "name": DOMAIN},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(set_site_domain, migrations.RunPython.noop),
    ]
