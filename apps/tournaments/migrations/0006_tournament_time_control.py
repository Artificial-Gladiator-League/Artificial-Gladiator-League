from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0005_tournamentparticipant_ready"),
    ]

    operations = [
        migrations.AddField(
            model_name="tournament",
            name="time_control",
            field=models.CharField(
                choices=[
                    ("1+0", "1+0 Bullet"),
                    ("2+1", "2+1 Bullet"),
                    ("3+0", "3+0 Blitz"),
                    ("3+1", "3+1 Blitz"),
                    ("3+2", "3+2 Blitz"),
                    ("5+0", "5+0 Blitz"),
                    ("5+3", "5+3 Blitz"),
                    ("10+0", "10+0 Rapid"),
                    ("10+5", "10+5 Rapid"),
                    ("15+10", "15+10 Rapid"),
                ],
                default="3+1",
                help_text="Time control for all games in this tournament.",
                max_length=10,
            ),
        ),
    ]
