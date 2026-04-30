"""Migration: add anti-cheat SHA audit infrastructure.

- TournamentParticipant.round_pinned_sha / round_pinned_at
- TournamentParticipant.disqualified_for_sha_mismatch
- New model TournamentShaCheck (full forensic audit log)
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0011_remove_prize_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="tournamentparticipant",
            name="round_pinned_sha",
            field=models.CharField(
                blank=True, default="", max_length=40,
                help_text=(
                    "Official pinned HF commit SHA captured at the start of "
                    "the current round. Compared against live HF SHA by the "
                    "random anti-cheat audit task."
                ),
            ),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="round_pinned_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="Timestamp when round_pinned_sha was last refreshed.",
            ),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="disqualified_for_sha_mismatch",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when the participant was disqualified by the "
                    "random mid-round SHA audit (repo changed during a "
                    "live round)."
                ),
            ),
        ),
        migrations.CreateModel(
            name="TournamentShaCheck",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("round_num", models.PositiveIntegerField(default=0, help_text="Tournament round when the check ran (0 = pre-start).")),
                ("game_type", models.CharField(blank=True, default="", max_length=20)),
                ("repo_id", models.CharField(blank=True, default="", max_length=255)),
                ("expected_sha", models.CharField(blank=True, default="", help_text="Pinned/baseline SHA at the start of the round.", max_length=40)),
                ("current_sha", models.CharField(blank=True, default="", help_text="SHA returned by the live HF Hub call.", max_length=40)),
                ("result", models.CharField(
                    choices=[("pass", "Pass"), ("fail", "Fail (mismatch)"), ("error", "Error (HF unreachable / no token)"), ("skipped", "Skipped (no baseline)")],
                    default="pass", max_length=10,
                )),
                ("context", models.CharField(
                    choices=[("round_start", "Round start baseline"), ("random_audit", "Random mid-round audit"), ("manual", "Manual / management command")],
                    default="random_audit", max_length=20,
                )),
                ("action_taken", models.CharField(blank=True, default="", help_text="e.g. 'disqualified_in_round', 'logged_only', 'forfeited_match'.", max_length=80)),
                ("error_message", models.TextField(blank=True, default="", help_text="Populated on Result=ERROR with the underlying exception text.")),
                ("checked_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("participant", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="sha_checks", to="tournaments.tournamentparticipant")),
                ("tournament", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="sha_checks", to="tournaments.tournament")),
                ("user", models.ForeignKey(
                    help_text="Denormalised — kept after participant deletion for audit.",
                    on_delete=models.deletion.CASCADE, related_name="sha_audit_entries",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "verbose_name": "Tournament SHA Check",
                "verbose_name_plural": "Tournament SHA Checks",
                "ordering": ["-checked_at"],
            },
        ),
        migrations.AddIndex(
            model_name="tournamentshacheck",
            index=models.Index(fields=["tournament", "round_num"], name="tournaments_t_tournam_e8c1bd_idx"),
        ),
        migrations.AddIndex(
            model_name="tournamentshacheck",
            index=models.Index(fields=["result", "-checked_at"], name="tournaments_t_result_3ad29f_idx"),
        ),
    ]
