from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0015_money_tournament'),
    ]

    operations = [
        migrations.AddField(
            model_name='tournamentparticipant',
            name='paypal_email',
            field=models.EmailField(
                blank=True,
                default='',
                help_text=(
                    'PayPal email address for prize payout. Collected at registration '
                    'for cash tournaments. Auto-cleared for non-winners at tournament end. '
                    'Admin must manually clear this for the winner after payout confirmation.'
                ),
                max_length=254,
            ),
        ),
    ]
