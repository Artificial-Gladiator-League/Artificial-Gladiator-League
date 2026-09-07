# Add "Test My Space" self-service verification fields to CustomUser.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0032_alter_data_space_sha_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='space_status',
            field=models.CharField(
                choices=[
                    ('unverified', 'Unverified'),
                    ('ok', 'OK'),
                    ('cold_start', 'Cold start'),
                    ('unreachable', 'Unreachable'),
                    ('bad_response', 'Bad response'),
                ],
                default='unverified',
                help_text="Result of the user's last 'Test My Space' get_move probe.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='customuser',
            name='space_last_verified_at',
            field=models.DateTimeField(
                blank=True,
                help_text="Timestamp of the last successful Space probe (status='ok').",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='customuser',
            name='space_last_latency_ms',
            field=models.IntegerField(
                blank=True,
                help_text='Round-trip latency (ms) measured during the last Space probe.',
                null=True,
            ),
        ),
    ]
