"""
Migration: add registered_sha and tournament_hf_token to TournamentParticipant.

registered_sha        — HF commit SHA captured at registration time.
                        Compared before each round to detect model changes.
                        Deleted at tournament end.

tournament_hf_token   — Read-only HF token stored for pre/mid-round checks.
                        Never used for writes; deleted at tournament end.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0009_add_game_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="tournamentparticipant",
            name="registered_sha",
            field=models.CharField(
                max_length=40,
                blank=True,
                default="",
                help_text=(
                    "HF commit SHA at tournament registration time. "
                    "Used to detect model changes during the tournament. "
                    "Deleted at tournament end."
                ),
            ),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="tournament_hf_token",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Read-only HF token stored for pre/mid-round integrity checks. "
                    "Never used for writes. Deleted at tournament end."
                ),
            ),
        ),
    ]
