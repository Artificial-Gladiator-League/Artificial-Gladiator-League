from django.db import migrations


def mark_pinned_models_active(apps, schema_editor):
    """0037 gave every existing row status='pending' (the field default).

    Models that already have an approved/pinned SHA were working in
    production before this deploy — treat them as ACTIVE so they don't
    show as unverified and don't get caught by any future ACTIVE-gated
    logic. Models with no pinned SHA are left as 'pending' (correct: they
    have never been through a contract check).
    """
    UserGameModel = apps.get_model('users', 'UserGameModel')
    UserGameModel.objects.filter(status='pending').exclude(
        approved_full_sha__isnull=True,
    ).exclude(approved_full_sha='').update(status='active')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0037_add_contract_status'),
    ]

    operations = [
        migrations.RunPython(mark_pinned_models_active, noop),
    ]
