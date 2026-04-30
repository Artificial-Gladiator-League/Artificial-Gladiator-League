"""Migration: probabilistic SHA audit bookkeeping fields.

Adds three fields to TournamentParticipant used by
``apps.tournaments.tasks.run_probabilistic_sha_audit`` for risk-based
random SHA spot checks:

  - last_sha_check_at:    when this participant was last audited
  - sha_anomaly_history:  has this participant ever triggered a (later
                          forgiven) SHA mismatch
  - disqualified_reason:  free-form audit trail when DQ'd
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0012_tournament_sha_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="tournamentparticipant",
            name="last_sha_check_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text=(
                    "Timestamp of the last probabilistic SHA audit "
                    "performed on this participant."
                ),
            ),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="sha_anomaly_history",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True if this participant has ever triggered a SHA "
                    "mismatch that was manually cleared/forgiven by an "
                    "admin. Increases probability of being audited again."
                ),
            ),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="disqualified_reason",
            field=models.TextField(
                blank=True, default="",
                help_text=(
                    "Free-form reason recorded when this participant was "
                    "disqualified."
                ),
            ),
        ),
    ]
