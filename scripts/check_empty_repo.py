import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','agladiator.settings')
import django
django.setup()
from apps.users.models import UserGameModel

qs = UserGameModel.objects.filter(hf_model_repo_id='')
print('Empty hf_model_repo_id count:', qs.count())
for gm in qs:
    print(gm.id, gm.user_id, gm.game_type, repr(gm.hf_model_repo_id))
