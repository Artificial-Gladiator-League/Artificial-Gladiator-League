"""Official Breakthrough handler — uses repo files but controlled by platform
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class EndpointHandler:
    """Official platform Breakthrough handler.

    This handler reads the user's repo directory (path) and imports the
    modules listed in `config_model.json` (if any). Unlike user-provided
    `handler.py`, this class is maintained by the platform and will always
    be used for Breakthrough inference.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.predict_fn = None
        self.zone_db = None
        self.module = None

        log.info("✅ Using OFFICIAL Breakthrough handler (ignoring user's handler.py)")
        log.info("✅ Loading model files from cache: %s", str(self.path))

        # Add repo dir to sys.path so relative imports work when loading
        # modules listed in config_model.json.
        if str(self.path) not in sys.path:
            sys.path.insert(0, str(self.path))

        # Load config_model.json if present and import modules listed there.
        model_config_path = self.path / "config_model.json"
        if model_config_path.exists():
            try:
                with open(model_config_path, encoding="utf-8") as fh:
                    model_config = json.load(fh)
            except Exception:
                model_config = {}
        else:
            model_config = {}

        main_module_name = model_config.get("main", "")
        for mod_file in model_config.get("files", []):
            mod_name = mod_file.removesuffix(".py")
            mod_path = self.path / mod_file
            if not mod_path.exists():
                log.warning("Module file %s not found — skipping.", mod_file)
                continue
            try:
                spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                log.info("Loaded module: %s", mod_name)
                if mod_name == main_module_name:
                    self.module = mod
            except Exception:
                log.exception("Failed importing module %s", mod_file)

        # Load zone DB from local file if present
        data_config_path = self.path / "config_data.json"
        if data_config_path.exists():
            try:
                with open(data_config_path, encoding="utf-8") as fh:
                    data_config = json.load(fh)
            except Exception:
                data_config = {}
        else:
            data_config = {}

        zone_db_filename = data_config.get("zone_db", "")
        if zone_db_filename:
            local_zone = self.path / zone_db_filename
            if local_zone.exists():
                try:
                    self.zone_db = np.load(str(local_zone), allow_pickle=True)
                    log.info("Zone DB loaded from local file %s", local_zone)
                except Exception:
                    log.exception("Failed loading zone DB %s", local_zone)

        # Initialise predictor
        self._init_predictor()

    def _init_predictor(self):
        if self.module is None:
            log.warning("No main module loaded — predictions will use random fallback.")
            return

        # Prefer get_move, then UCTSearcher, then predict
        if hasattr(self.module, "get_move"):
            raw_fn = self.module.get_move
            self.predict_fn = lambda fen, player: raw_fn(fen, player, zone_db=self.zone_db)
            log.info("Using module.get_move() for predictions.")
            return

        if hasattr(self.module, "UCTSearcher"):
            searcher_cls = self.module.UCTSearcher
            kwargs = {}
            if self.zone_db is not None:
                kwargs["zone_db"] = self.zone_db
            searcher = searcher_cls(**kwargs)
            self.predict_fn = lambda fen, player: searcher.search(fen, player)
            log.info("Using module.UCTSearcher for predictions.")
            return

        if hasattr(self.module, "predict"):
            raw_fn = self.module.predict
            self.predict_fn = lambda fen, player: raw_fn(fen, player)
            log.info("Using module.predict() for predictions.")
            return

        log.warning("Module %s has no get_move / UCTSearcher / predict — using random fallback.", self.module.__name__)

    def __call__(self, data: dict) -> dict:
        inputs = data.get("inputs", data)
        fen = inputs.get("fen", "")
        player = inputs.get("player", "w")

        if not fen:
            return {"error": "Missing 'fen' in request."}

        if self.predict_fn is not None:
            try:
                move = self.predict_fn(fen, player)
                if move and isinstance(move, str):
                    return {"move": move}
            except Exception:
                log.exception("Model prediction failed for FEN=%s", fen)

        # Fallback random (simple) - prefer captures
        moves = self._legal_moves(fen)
        if not moves:
            return {"error": "No legal moves available.", "move": "0000"}
        captures = [m for m in moves if m[0] != m[2]]
        move = captures[0] if captures else moves[0]
        return {"move": move}

    @staticmethod
    def _legal_moves(fen: str) -> list[str]:
        parts = fen.strip().split()
        turn = parts[1] if len(parts) > 1 else "w"
        grid = []
        for rank_str in parts[0].split("/"):
            row = []
            for ch in rank_str:
                if ch.isdigit():
                    row.extend(["."] * int(ch))
                else:
                    row.append(ch)
            grid.append(row)
        moves = []
        files = "abcdefgh"
        ranks = "12345678"
        piece = "W" if turn == "w" else "B"
        direction = -1 if turn == "w" else 1
        for r in range(8):
            for c in range(8):
                if grid[r][c] != piece:
                    continue
                nr = r + direction
                if nr < 0 or nr >= 8:
                    continue
                if grid[nr][c] == ".":
                    moves.append(f"{files[c]}{ranks[7-r]}{files[c]}{ranks[7-nr]}")
                for dc in (-1, 1):
                    nc = c + dc
                    if 0 <= nc < 8 and grid[nr][nc] != piece:
                        moves.append(f"{files[c]}{ranks[7-r]}{files[nc]}{ranks[7-nr]}")
        return moves
