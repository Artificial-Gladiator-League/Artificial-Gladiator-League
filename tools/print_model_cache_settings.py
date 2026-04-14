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

print('MODEL_CACHE_ROOT =', getattr(settings, 'MODEL_CACHE_ROOT', None))
print('USER_MODELS_BASE_DIR =', getattr(settings, 'USER_MODELS_BASE_DIR', None))
print('HF_HOME env =', os.environ.get('HF_HOME'))
print('HF_HUB_CACHE env =', os.environ.get('HF_HUB_CACHE'))
print('ALLOW_PER_MOVE_DOWNLOADS =', getattr(settings, 'ALLOW_PER_MOVE_DOWNLOADS', None))

root = getattr(settings, 'MODEL_CACHE_ROOT', None)
if root:
    p = Path(root)
    print('exists:', p.exists())
    try:
        print('resolved:', p.resolve())
    except Exception as e:
        print('resolve error:', e)
    if p.exists():
        try:
            print('children:', [c.name for c in sorted(p.iterdir())][:20])
        except Exception as e:
            print('list error:', e)
