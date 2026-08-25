from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0017_tournament_join_password'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournament',
            name='prize_structure',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    'Prize breakdown as a JSON list, e.g. '
                    '[{"place": 1, "label": "1st", "amount": 60}, '
                    '{"place": 2, "label": "2nd", "amount": 30}, '
                    '{"place": 3, "label": "3rd", "amount": 10}]'
                ),
            ),
        ),
    ]
