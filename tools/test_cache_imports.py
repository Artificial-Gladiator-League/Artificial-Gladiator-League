#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# Ensure project parent is on sys.path so package imports work when running
# the script directly from tools/.
here = Path(__file__).resolve().parent
project_dir = here.parent
if str(project_dir) not in sys.path:
    sys.path.insert(0, str(project_dir))

# Ensure Django settings are configured
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agladiator.settings')
try:
    import django
    django.setup()
except Exception as e:
    print('django_setup_error:', e)
    sys.exit(2)

from importlib import import_module
results = {}

# Check signals
try:
    m = import_module('apps.users.signals')
    results['signals'] = 'ok'
    results['signals_names'] = [n for n in dir(m) if 'login' in n.lower() or 'logout' in n.lower() or 'preload' in n.lower() or 'clear' in n.lower()]
except Exception as e:
    results['signals'] = f'err: {e}'

# Check model_lifecycle helpers
try:
    ml = import_module('apps.users.model_lifecycle')
    results['model_lifecycle'] = 'ok'
    results['has_download_to_cache'] = hasattr(ml, 'download_model_to_cache')
    results['has_get_user_cache_dir'] = hasattr(ml, 'get_user_model_cache_dir')
except Exception as e:
    results['model_lifecycle'] = f'err: {e}'

# Check local_sandbox_inference changes
try:
    lsi = import_module('apps.games.local_sandbox_inference')
    results['local_sandbox_inference'] = 'ok'
    results['find_cached_model'] = hasattr(lsi, '_find_cached_model')
    results['verify_model'] = hasattr(lsi, 'verify_model')
except Exception as e:
    results['local_sandbox_inference'] = f'err: {e}'

print(results)
