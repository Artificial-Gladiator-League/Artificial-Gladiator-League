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

user_id = sys.argv[1] if len(sys.argv) > 1 else '98'
game_type = sys.argv[2] if len(sys.argv) > 2 else 'breakthrough'

root = getattr(settings, 'MODEL_CACHE_ROOT', None)
if not root:
    print('No MODEL_CACHE_ROOT')
    sys.exit(1)

path = Path(root) / f'user_{user_id}' / game_type
print('Listing:', path)
if not path.exists():
    print('Path does not exist')
    sys.exit(0)

for item in sorted(path.iterdir()):
    try:
        print('->', item.name, 'dir=', item.is_dir())
        if item.is_dir():
            files = sorted([c.name for c in item.rglob('*') if c.is_file()])
            print('   files (first 20):', files[:20])
    except Exception as e:
        print('   error listing', item, e)
