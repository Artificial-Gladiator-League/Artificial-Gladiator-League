from django.core.management.base import BaseCommand

from apps.users.guest_lifecycle import purge_expired_guests


class Command(BaseCommand):
    help = "Delete guest accounts that were idle for GUEST_TTL_DAYS (account, repos and files)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be deleted without deleting.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        report = purge_expired_guests(dry_run=dry_run)
        for entry in report:
            self.stdout.write(
                f"{'[dry-run] ' if dry_run else ''}{entry['action']}: id={entry['id']} "
                f"username={entry['username']} expired={entry['expired']}"
            )
        deleted = sum(1 for e in report if e["action"] in ("delete", "abort games and delete"))
        self.stdout.write(f"{'Would delete' if dry_run else 'Deleted'} {deleted} of {len(report)} expired guest(s).")
