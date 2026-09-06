# Add registration-time data-repo and space SHA baselines to participants.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0024_gauntlet_terms_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournamentparticipant',
            name='registered_data_repo_sha',
            field=models.CharField(blank=True, default='', help_text='HF data-repo commit SHA at registration time. Baseline for the registration-period audit. Deleted at tournament end.', max_length=64),
        ),
        migrations.AddField(
            model_name='tournamentparticipant',
            name='registered_space_sha',
            field=models.CharField(blank=True, default='', help_text='HF Space commit SHA at registration time. Baseline for the registration-period audit. Deleted at tournament end.', max_length=64),
        ),
    ]
