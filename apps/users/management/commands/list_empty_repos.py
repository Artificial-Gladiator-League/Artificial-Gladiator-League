from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "List UserGameModel entries with empty hf_model_repo_id"

    def handle(self, *args, **options):
        try:
            from apps.users.models import UserGameModel
        except Exception as exc:
            self.stderr.write(f"Import error: {exc}")
            return

        qs = UserGameModel.objects.filter(hf_model_repo_id='')
        self.stdout.write(f"Empty hf_model_repo_id count: {qs.count()}")
        for gm in qs:
            self.stdout.write(
                f"id={gm.id} user_id={gm.user_id} game_type={gm.game_type} hf_model_repo_id={repr(gm.hf_model_repo_id)} cached_path={getattr(gm, 'cached_path', '')}"
            )
