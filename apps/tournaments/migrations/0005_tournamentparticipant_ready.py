from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0004_add_qa_tournament_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournamentparticipant',
            name='ready',
            field=models.BooleanField(
                default=False,
                help_text='Whether the player has clicked Ready (used by QA tournaments).',
            ),
        ),
    ]
