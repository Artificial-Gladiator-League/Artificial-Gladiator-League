from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0042_guest_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='usergamemodel',
            name='prepare_stage',
            field=models.CharField(
                blank=True, default='', max_length=20,
                help_text='Guest accounts: step of the running download/check (downloading_model, downloading_data, checking).',
            ),
        ),
    ]
