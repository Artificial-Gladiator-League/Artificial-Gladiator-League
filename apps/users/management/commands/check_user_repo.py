from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Show resolved repo from bot_runner._get_repo_for_user for a given user"

    def add_arguments(self, parser):
        parser.add_argument('--user-id', type=int, required=True)
        parser.add_argument('--game-type', type=str, default='breakthrough')

    def handle(self, *args, **options):
        user_id = options['user_id']
        game_type = options['game_type']
        try:
            from apps.users.models import CustomUser
            from apps.games.bot_runner import _get_repo_for_user
        except Exception as exc:
            self.stderr.write(f"Import error: {exc}")
            return

        try:
            user = CustomUser.objects.get(pk=user_id)
        except CustomUser.DoesNotExist:
            self.stderr.write(f"User id={user_id} not found")
            return

        repo = _get_repo_for_user(user, game_type)
        self.stdout.write(f"Resolved repo for user {user.username} (id={user_id}) game_type={game_type}: {repr(repo)}")
