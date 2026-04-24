#!/usr/bin/env python3
import os
import sys
import logging

# Ensure Django settings are configured
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agladiator.settings')
import django
django.setup()

# Configure root logger to print INFO+ to stdout for this run
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(name)s: %(message)s')

from apps.users.models import UserGameModel
from apps.games.predict_breakthrough import get_move

USER_ID = 130

gm = UserGameModel.objects.filter(user_id=USER_ID, game_type='breakthrough').first()
if not gm:
    print(f"No UserGameModel found for user_id={USER_ID} (breakthrough)")
    sys.exit(1)

print(f"Found UserGameModel: id={gm.id} repo={gm.hf_model_repo_id} cached_path={gm.cached_path} sandbox_verified={gm.sandbox_verified}")

fen = "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w"
print("Calling Breakthrough get_move() — watch logs for 'using local process' vs 'random fallback'...")
move = get_move(fen, 'w', hf_repo_id=gm.hf_model_repo_id)
print(f"get_move returned: {move}")
