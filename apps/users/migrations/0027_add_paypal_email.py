from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0026_add_per_field_ownership_verified'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='paypal_email',
            field=models.EmailField(
                blank=True,
                default='',
                help_text=(
                    'PayPal email address for cash-tournament prize payouts. '
                    'Set by the user in their profile. Never used as a login credential.'
                ),
                max_length=254,
            ),
        ),
    ]
