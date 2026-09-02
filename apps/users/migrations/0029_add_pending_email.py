from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0028_usergamemodel_repo_changed'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='pending_email',
            field=models.EmailField(
                blank=True,
                null=True,
                help_text='Unconfirmed new email awaiting confirmation from the user.',
            ),
        ),
    ]
