from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0016_add_paypal_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournament',
            name='join_password',
            field=models.CharField(
                blank=True,
                null=True,
                max_length=50,
                help_text='Leave empty for public tournaments. Set to require a password to join.',
            ),
        ),
    ]
