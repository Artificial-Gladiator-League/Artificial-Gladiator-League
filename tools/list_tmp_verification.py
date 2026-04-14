#!/usr/bin/env python3
from pathlib import Path
p = Path('/tmp/verification')
print('path:', p)
print('exists:', p.exists())
if p.exists():
    for c in sorted(p.iterdir()):
        print('-', c.name, 'is_dir=', c.is_dir())
        if c.is_dir():
            try:
                print('   children:', [x.name for x in sorted(c.iterdir())][:40])
            except Exception as e:
                print('   error listing:', e)