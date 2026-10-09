import logging

from django.core.management.base import BaseCommand

from apps.games.local_inference import _hf_cache_root
from apps.users.model_lifecycle import _hf_cache_folder_name, _rmtree_with_retries

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete models--*/datasets--* folders in HF_HUB_CACHE that no user references."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List folders without deleting.")

    def handle(self, *args, **options):
        from apps.users.models import CustomUser, UserGameModel

        dry_run = options["dry_run"]
        models_used = set()
        data_used = set()
        for model_id, data_id in UserGameModel.objects.values_list("hf_model_repo_id", "hf_data_repo_id"):
            if model_id:
                models_used.add(_hf_cache_folder_name("models", model_id.strip()).lower())
            if data_id:
                data_used.add(_hf_cache_folder_name("datasets", data_id.strip()).lower())
        for legacy in CustomUser.objects.exclude(hf_model_repo_id="").values_list("hf_model_repo_id", flat=True):
            models_used.add(_hf_cache_folder_name("models", legacy.strip()).lower())
        referenced = models_used | data_used

        root = _hf_cache_root()
        if not root.is_dir():
            self.stdout.write(f"HF cache not found: {root}")
            return

        count = 0
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not entry.name.startswith(("models--", "datasets--")):
                continue
            if entry.name.lower() in referenced:
                continue
            count += 1
            if dry_run:
                self.stdout.write(f"[dry-run] would delete {entry}")
            elif _rmtree_with_retries(entry):
                log.info("cleanup_hf_cache: deleted unreferenced %s", entry)
                self.stdout.write(f"deleted {entry}")
            else:
                self.stderr.write(f"could not delete {entry}")
        self.stdout.write(f"{'Would delete' if dry_run else 'Processed'} {count} unreferenced folder(s).")
