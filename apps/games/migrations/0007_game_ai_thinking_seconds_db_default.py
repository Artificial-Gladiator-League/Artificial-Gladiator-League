# Sets a database-level DEFAULT on ai_thinking_seconds so MySQL strict mode
# does not raise IntegrityError 1364 when the column is omitted in an INSERT.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("games", "0006_game_ai_thinking_seconds"),
    ]

    operations = [
        migrations.AlterField(
            model_name="game",
            name="ai_thinking_seconds",
            field=models.FloatField(
                default=1.0,
                db_default=1.0,
                help_text="Maximum seconds allowed for AI to think per move.",
            ),
        ),
    ]
