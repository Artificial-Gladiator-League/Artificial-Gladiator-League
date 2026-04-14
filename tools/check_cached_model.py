#!/usr/bin/env python3
"""Check UserGameModel cache entries for a given HF repo.

Usage: python tools/check_cached_model.py <repo_id>
"""
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
project_dir = here.parent
if str(project_dir) not in sys.path:
    sys.path.insert(0, str(project_dir))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agladiator.settings')
try:
    import django
    django.setup()
except Exception as e:
    print('django_setup_error:', e)
    raise

from apps.users.models import UserGameModel

repo = sys.argv[1] if len(sys.argv) > 1 else 'chaim-duchovny/breakthrough-model'
rows = list(UserGameModel.objects.filter(hf_model_repo_id=repo))
print(f'Query for repo={repo} — found {len(rows)} rows')

for gm in rows:
    print('---')
    print('id:', gm.pk)
    print('user_id:', gm.user_id)
    try:
        print('username:', gm.user.username)
    except Exception:
        pass
    print('game_type:', gm.game_type)
    print('model_integrity_ok:', gm.model_integrity_ok)
    print('verification_status:', gm.verification_status)
    print('last_verified_commit:', gm.last_verified_commit)
    print('cached_path:', repr(gm.cached_path))
    print('cached_at:', gm.cached_at)
    print('cached_commit:', gm.cached_commit)

    cp = gm.cached_path
    if cp:
        p = Path(cp)
        print('cached_path exists on disk:', p.exists())
        try:
            print('resolved:', p.resolve())
        except Exception as e:
            print('resolve error:', e)
        if p.exists() and p.is_dir():
            try:
                items = sorted([str(x.name) for x in p.iterdir()])
                print('contents (first 20):', items[:20])
            except Exception as e:
                print('listing error:', e)
    else:
        print('no cached_path set for this row')

if not rows:
    print('No UserGameModel rows found for this repo. Check CustomUser.hf_model_repo_id or verify login-time task ran.')
