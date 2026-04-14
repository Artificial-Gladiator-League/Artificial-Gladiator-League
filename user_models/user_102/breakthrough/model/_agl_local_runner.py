#!/usr/bin/env python3
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path

MODEL_DIR = Path(os.environ.get('MODEL_DIR', 'C:/Users/User.COMPUTER3/Desktop/Draft/agladiator/agladiator/user_models/user_102/breakthrough/model'))
DATA_DIR = Path(os.environ.get('DATA_DIR', '')) if os.environ.get('DATA_DIR') else None
FEN = os.environ.get('FEN', '')
PLAYER = os.environ.get('PLAYER', 'w')
GAME_TYPE = os.environ.get('GAME_TYPE', 'breakthrough')

def load_zone_db():
    # Try config_model.json zone_db_filename, else 'zone_db.npz'
    cfg = MODEL_DIR / 'config_model.json'
    filename = 'zone_db.npz'
    try:
        if cfg.exists():
            import json
            d = json.loads(cfg.read_text(encoding='utf-8'))
            filename = d.get('zone_db_filename', filename)
    except Exception:
        pass
    # Prefer DATA_DIR, then MODEL_DIR
    for base in (DATA_DIR, MODEL_DIR):
        if base:
            p = base / filename
            if p.exists():
                try:
                    import numpy as np
                    return np.load(str(p), allow_pickle=True)
                except Exception:
                    return None
    return None

def try_handler_get_move():
    # Prefer the platform's official handlers (mounted into HANDLERS_DIR).
    HANDLERS_DIR = Path('C:/Users/User.COMPUTER3/Desktop/Draft/agladiator/agladiator/apps/games/handlers')
    try:
        if HANDLERS_DIR.exists() and str(HANDLERS_DIR) not in sys.path:
            sys.path.insert(0, str(HANDLERS_DIR))
    except Exception:
        pass

    try:
        if GAME_TYPE == 'breakthrough':
            import official_breakthrough_handler as oh
            print("✅ Using OFFICIAL Breakthrough handler (ignoring user handler.py)", file=sys.stderr)
        else:
            import official_chess_handler as oh
            print("✅ Using OFFICIAL Chess handler (ignoring user handler.py)", file=sys.stderr)

        handler = oh.EndpointHandler(path=str(MODEL_DIR))
        print(f'✅ Loading model weights from cache: {MODEL_DIR}', file=sys.stderr)
        return handler({ 'inputs': {'fen': FEN, 'player': PLAYER, 'game_type': GAME_TYPE} })
    except Exception:
        # Fall back to user modules/config if official handler fails — keep best-effort compatibility
        pass

    # Legacy: if no official handler or it failed, try loading user modules from config
    handler_path = MODEL_DIR / 'handler.py'
    if not handler_path.exists():
        return None
    spec = importlib.util.spec_from_file_location('user_handler', str(handler_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1) top-level get_move
    if hasattr(mod, 'get_move'):
        fn = getattr(mod, 'get_move')
        try:
            zone_db = load_zone_db()
            try:
                return fn(FEN, PLAYER, zone_db=zone_db)
            except TypeError:
                return fn(FEN, PLAYER)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return None

    # 2) EndpointHandler
    if hasattr(mod, 'EndpointHandler'):
        try:
            handler = mod.EndpointHandler(path=str(MODEL_DIR))
            return handler({ 'inputs': {'fen': FEN, 'player': PLAYER, 'game_type': GAME_TYPE} })
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return None

    return None

def try_modules_from_config():
    cfg = MODEL_DIR / 'config_model.json'
    modules = []
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding='utf-8'))
            modules = data.get('modules', [])
        except Exception:
            pass

    for mfile in modules:
        mpath = MODEL_DIR / mfile
        if not mpath.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(mpath.stem, str(mpath))
            mm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mm)
            # get_move
            if hasattr(mm, 'get_move'):
                fn = getattr(mm, 'get_move')
                try:
                    zone_db = load_zone_db()
                    try:
                        return fn(FEN, PLAYER, zone_db=zone_db)
                    except TypeError:
                        return fn(FEN, PLAYER)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    continue
            # UCTSearcher
            if hasattr(mm, 'UCTSearcher'):
                try:
                    cls = getattr(mm, 'UCTSearcher')
                    zone_db = load_zone_db()
                    kwargs = {} if zone_db is None else {'zone_db': zone_db}
                    s = cls(**kwargs)
                    return s.search(FEN, PLAYER)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    continue
            # predict
            if hasattr(mm, 'predict'):
                try:
                    fn = getattr(mm, 'predict')
                    return fn(FEN, PLAYER)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    continue
        except Exception:
            traceback.print_exc(file=sys.stderr)
            continue
    return None

def main():
    # Try handler first
    out = try_handler_get_move()
    if out is None:
        out = try_modules_from_config()
    if out is None:
        print('ERROR:no_result', file=sys.stderr)
        sys.exit(2)

    move = None
    if isinstance(out, str):
        move = out.strip()
    elif isinstance(out, dict):
        move = out.get('move') or out.get('output')
    elif isinstance(out, list) and out:
        it = out[0]
        if isinstance(it, dict):
            move = it.get('move') or it.get('output')
        elif isinstance(it, str):
            move = it.strip()

    if move:
        print(f'MOVE:{move}', flush=True)
        sys.exit(0)
    else:
        print('ERROR:empty_result', file=sys.stderr)
        sys.exit(3)

if __name__ == '__main__':
    main()
