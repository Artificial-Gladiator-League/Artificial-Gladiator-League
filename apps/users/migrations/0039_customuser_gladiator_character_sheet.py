from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0038_backfill_active_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='origin_story',
            field=models.TextField(blank=True, default='', max_length=1000),
        ),
        migrations.AddField(
            model_name='customuser',
            name='fighting_style',
            field=models.CharField(
                choices=[
                    ('Unhinged Rage', 'Unhinged Rage'),
                    ('Silent Assassin', 'Silent Assassin'),
                    ('The Rage Philosopher', 'The Rage Philosopher'),
                    ('Chaotic Genius', 'Chaotic Genius'),
                ],
                default='Unhinged Rage',
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name='customuser',
            name='current_mood',
            field=models.CharField(default='😈', max_length=8),
        ),
    ]
