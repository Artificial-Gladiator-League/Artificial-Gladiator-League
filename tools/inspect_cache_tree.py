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

root = getattr(settings, 'MODEL_CACHE_ROOT', None)
print('MODEL_CACHE_ROOT =', root)
if not root:
    sys.exit(1)
rootp = Path(root)
print('root exists:', rootp.exists())
if not rootp.exists():
    sys.exit(1)

for child in sorted(rootp.iterdir()):
    try:
        print('==', child.name, 'dir=', child.is_dir())
        if child.is_dir():
            subs = sorted([c.name for c in child.iterdir()])
            print('   subs:', subs[:40])
            # If this is a user_* folder, inspect deeper
            if child.name.startswith('user_') or child.name.isdigit():
                for g in child.iterdir():
                    if g.is_dir():
                        print('     -', g.name)
    except Exception as e:
        print('   error listing', child, e)
