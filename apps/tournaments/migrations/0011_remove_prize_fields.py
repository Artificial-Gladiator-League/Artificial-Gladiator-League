"""
Migration: drop prize_pool and rollover_amount from Tournament.

Cash prizes have been removed from the platform entirely.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0010_tournamentparticipant_add_integrity_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="tournament",
            name="prize_pool",
        ),
        migrations.RemoveField(
            model_name="tournament",
            name="rollover_amount",
        ),
    ]
