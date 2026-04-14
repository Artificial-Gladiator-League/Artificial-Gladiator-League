#!/usr/bin/env python3
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
proj = here.parent
if str(proj) not in sys.path:
    sys.path.insert(0, str(proj))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agladiator.settings')
import django
django.setup()
from django.conf import settings

repo = sys.argv[1] if len(sys.argv) > 1 else 'chaim-duchovny/breakthrough-model'
repo_folder = repo.replace('/', '__')
root = getattr(settings, 'MODEL_CACHE_ROOT', None)
print('Searching MODEL_CACHE_ROOT:', root)
if not root:
    print('No MODEL_CACHE_ROOT configured')
    sys.exit(1)

rootp = Path(root)
if not rootp.exists():
    print('MODEL_CACHE_ROOT does not exist on disk:', rootp)
    sys.exit(1)

matches = list(rootp.rglob('*' + repo_folder + '*'))
print('Found', len(matches), 'matches for', repo_folder)
for m in matches:
    try:
        print('-', m, 'is_dir=', m.is_dir())
        if m.is_dir():
            print('  children:', [c.name for c in sorted(m.iterdir())][:20])
    except Exception as e:
        print('  error listing', m, e)
