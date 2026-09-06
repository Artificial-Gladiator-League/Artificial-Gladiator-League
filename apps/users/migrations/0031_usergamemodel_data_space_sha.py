# Generated for data-repo and HF Space SHA tracking.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0030_usergamemodel_current_repo_sha_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='usergamemodel',
            name='approved_data_repo_sha',
            field=models.CharField(blank=True, help_text='Exact immutable data-repo commit SHA approved at submission.', max_length=40),
        ),
        migrations.AddField(
            model_name='usergamemodel',
            name='current_data_repo_sha',
            field=models.CharField(blank=True, help_text='SHA of the data repo at the time it was last verified/approved.', max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='usergamemodel',
            name='new_data_repo_sha',
            field=models.CharField(blank=True, help_text='SHA of the incoming data repo when a change is detected.', max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='usergamemodel',
            name='approved_space_sha',
            field=models.CharField(blank=True, help_text='Exact immutable HF Space commit SHA approved at submission.', max_length=40),
        ),
        migrations.AddField(
            model_name='usergamemodel',
            name='current_space_sha',
            field=models.CharField(blank=True, help_text='SHA of the HF Space repo at the time it was last verified/approved.', max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='usergamemodel',
            name='new_space_sha',
            field=models.CharField(blank=True, help_text='SHA of the incoming HF Space repo when a change is detected.', max_length=64, null=True),
        ),
    ]
