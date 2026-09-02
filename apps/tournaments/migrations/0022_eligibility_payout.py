import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tournaments', '0021_thinking_time_choices_update'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Eligibility self-declaration fields on TournamentParticipant ──
        migrations.AddField(
            model_name='tournamentparticipant',
            name='confirmed_age_18_plus',
            field=models.BooleanField(
                default=False,
                help_text='Participant self-declared they are 18 or older at the time of joining.',
            ),
        ),
        migrations.AddField(
            model_name='tournamentparticipant',
            name='confirmed_israeli_resident',
            field=models.BooleanField(
                default=False,
                help_text='Participant self-declared Israeli residency at the time of joining.',
            ),
        ),
        migrations.AddField(
            model_name='tournamentparticipant',
            name='eligibility_confirmed_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Timestamp when the participant submitted the eligibility self-declarations.',
            ),
        ),
        # ── EligibilityVerification model ─────────────────────────────────
        migrations.CreateModel(
            name='EligibilityVerification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('verified', 'Verified'), ('rejected', 'Rejected')],
                    db_index=True,
                    default='pending',
                    max_length=10,
                )),
                ('verification_method', models.CharField(
                    blank=True,
                    help_text=(
                        "How the check was performed, e.g. 'video call' or "
                        "'live ID review, not retained'. No file upload."
                    ),
                    max_length=200,
                )),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('tournament_entry', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='eligibility_verification',
                    to='tournaments.tournamentparticipant',
                )),
                ('verified_by', models.ForeignKey(
                    blank=True,
                    limit_choices_to={'is_staff': True},
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Eligibility Verification',
                'verbose_name_plural': 'Eligibility Verifications',
            },
        ),
        migrations.AddIndex(
            model_name='eligibilityverification',
            index=models.Index(fields=['status'], name='tournaments_eligib_status_idx'),
        ),
        # ── PayoutConfirmation model ──────────────────────────────────────
        migrations.CreateModel(
            name='PayoutConfirmation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('paypal_email_snapshot', models.EmailField(
                    help_text="Copy of the user's PayPal email at the moment they confirmed.",
                )),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('confirmed_by_user', models.BooleanField(default=False)),
                ('tournament_entry', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='payout_confirmation',
                    to='tournaments.tournamentparticipant',
                )),
            ],
            options={
                'verbose_name': 'Payout Confirmation',
                'verbose_name_plural': 'Payout Confirmations',
            },
        ),
        migrations.AddIndex(
            model_name='payoutconfirmation',
            index=models.Index(fields=['confirmed_by_user'], name='tournaments_payout_confirmed_idx'),
        ),
    ]
