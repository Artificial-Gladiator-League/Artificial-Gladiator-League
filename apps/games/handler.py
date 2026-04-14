# ──────────────────────────────────────────────
# sample_breakthrough_handler.py
#
# Sample custom handler for HF Inference Endpoints.
#
# *** COPY THIS FILE INTO YOUR BREAKTHROUGH HF
#     MODEL REPO AS ``handler.py``. ***
#
# When HF Inference Endpoints detects a handler.py
# in the repo root, it uses this class to serve
# predictions instead of the default Transformers
# pipeline.
#
# Expected repo layout:
#
#   your-username/breakthrough-model/
#   ├── handler.py              ← this file (rename to handler.py)
#   ├── config_model.json       ← lists your Python modules
#   ├── breakthrough_mcvs.py    ← your UCT search / eval module
#   └── requirements.txt        ← runtime deps (numpy, huggingface_hub)
#
# And if you have a separate data repo for the zone DB:
#
#   your-username/breakthrough-data/
#   └── zone_db.npz
#
# config_model.json example:
#
#   {
#       "modules": ["breakthrough_mcvs.py"],
#       "data_repo_id": "your-username/breakthrough-data",
#       "zone_db_filename": "zone_db.npz"
#   }
#
# If the zone DB lives in your model repo instead
# of a separate data repo, omit "data_repo_id" and
# place zone_db.npz alongside handler.py.
#
# Request format (sent by Agladiator):
#
#   POST /
#   {
#       "inputs": {
#           "fen": "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w",
#           "player": "w",
#           "game_type": "breakthrough"
#       }
#   }
#
# Response format (what you must return):
#
#   {"move": "a2a3"}
#
# ──────────────────────────────────────────────
from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Breakthrough board constants (must match the
#  server's breakthrough_engine.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOARD_SIZE = 8
_FILES = "abcdefgh"
_RANKS = "12345678"
WHITE, BLACK = "w", "b"
WHITE_PIECE, BLACK_PIECE, EMPTY = "W", "B", "."


class EndpointHandler:
    """Custom handler for Breakthrough AI on HF Inference Endpoints.

    Lifecycle:
      1. ``__init__`` is called once when the endpoint container starts.
         Use it to load your model artifacts (modules, zone DB, etc.).
      2. ``__call__`` is called for every inference request.
         It receives the parsed JSON body and must return a dict.
    """

    def __init__(self, path: str):
        """Load model artefacts from *path* (the cloned repo directory).

        Steps:
          1. Read ``config_model.json`` to discover module files.
          2. Dynamically import each listed module.
          3. Download / load the zone database.
          4. Initialise the search engine.
        """
        self.path = Path(path)
        self.predict_fn = None
        self.zone_db = None
        self.module = None

        # ── 1. Read config ──────────────────────
        config_path = self.path / "config_model.json"
        if not config_path.exists():
            log.warning("No config_model.json found in %s", path)
            self.config: dict = {}
        else:
            with open(config_path) as fh:
                self.config = json.load(fh)

        # ── 2. Import listed modules ────────────
        # Add repo dir to sys.path so relative imports work.
        if str(self.path) not in sys.path:
            sys.path.insert(0, str(self.path))

        for mod_file in self.config.get("modules", []):
            mod_name = mod_file.removesuffix(".py")
            mod_path = self.path / mod_file
            if not mod_path.exists():
                log.warning("Module file %s not found — skipping.", mod_file)
                continue
            spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            # Keep the last loaded module as the "main" module.
            self.module = mod
            log.info("Loaded module: %s", mod_name)

        # ── 3. Load zone database ───────────────
        zone_db_filename = self.config.get("zone_db_filename", "zone_db.npz")

        # Check env var first (set by Agladiator platform), then config.
        data_repo = os.environ.get(
            "HF_DATA_REPO_ID",
            self.config.get("data_repo_id", ""),
        )

        if data_repo:
            # Download from the linked HF dataset repo.
            try:
                from huggingface_hub import hf_hub_download

                zone_path = hf_hub_download(
                    repo_id=data_repo,
                    filename=zone_db_filename,
                    repo_type="dataset",
                )
                self.zone_db = np.load(zone_path, allow_pickle=True)
                log.info("Zone DB loaded from data repo %s", data_repo)
            except Exception:
                log.exception(
                    "Failed to download zone DB from %s — "
                    "falling back to local file.",
                    data_repo,
                )

        # Fall back to a local copy in the model repo.
        if self.zone_db is None:
            local_zone = self.path / zone_db_filename
            if local_zone.exists():
                self.zone_db = np.load(str(local_zone), allow_pickle=True)
                log.info("Zone DB loaded from local file %s", local_zone)
            else:
                log.warning(
                    "No zone DB found (checked data repo '%s' and local '%s').",
                    data_repo,
                    local_zone,
                )

        # Fall back to DATA_DIR env var (set by Agladiator Docker sandbox
        # when a separate data repository is mounted at /data).
        if self.zone_db is None:
            data_dir_env = os.environ.get("DATA_DIR", "")
            if data_dir_env:
                data_zone = Path(data_dir_env) / zone_db_filename
                if data_zone.exists():
                    self.zone_db = np.load(str(data_zone), allow_pickle=True)
                    log.info("Zone DB loaded from DATA_DIR %s", data_zone)

        # ── 4. Initialise the predictor ─────────
        self._init_predictor()
        log.info("EndpointHandler initialised successfully.")

    # ──────────────────────────────────────────
    #  Predictor initialisation
    # ──────────────────────────────────────────
    def _init_predictor(self):
        """Discover and configure the prediction function from the
        loaded module.  Tries several common patterns:

        1. ``module.get_move(fen, player, zone_db=...)``
        2. ``module.UCTSearcher(zone_db=...).search(fen, player)``
        3. ``module.predict(fen, player)``
        """
        if self.module is None:
            log.warning("No module loaded — predictions will use random fallback.")
            return

        # Pattern 1: module exposes a top-level get_move()
        if hasattr(self.module, "get_move"):
            raw_fn = self.module.get_move
            self.predict_fn = lambda fen, player: raw_fn(
                fen, player, zone_db=self.zone_db
            )
            log.info("Using module.get_move() for predictions.")
            return

        # Pattern 2: module exposes a UCTSearcher class
        if hasattr(self.module, "UCTSearcher"):
            searcher_cls = self.module.UCTSearcher
            kwargs = {}
            if self.zone_db is not None:
                kwargs["zone_db"] = self.zone_db
            searcher = searcher_cls(**kwargs)
            self.predict_fn = lambda fen, player: searcher.search(fen, player)
            log.info("Using module.UCTSearcher for predictions.")
            return

        # Pattern 3: module exposes a predict()
        if hasattr(self.module, "predict"):
            raw_fn = self.module.predict
            self.predict_fn = lambda fen, player: raw_fn(fen, player)
            log.info("Using module.predict() for predictions.")
            return

        log.warning(
            "Module %s has no get_move / UCTSearcher / predict — "
            "predictions will use random fallback.",
            self.module.__name__,
        )

    # ──────────────────────────────────────────
    #  Inference entry point
    # ──────────────────────────────────────────
    def __call__(self, data: dict) -> dict:
        """Handle an inference request.

        Expected *data*::

            {"inputs": {"fen": "...", "player": "w"}}

        Returns::

            {"move": "a2a3"}

        Falls back to a random legal move if the model fails.
        """
        inputs = data.get("inputs", data)
        fen = inputs.get("fen", "")
        player = inputs.get("player", WHITE)

        if not fen:
            return {"error": "Missing 'fen' in request."}

        # Try model prediction
        if self.predict_fn is not None:
            try:
                move = self.predict_fn(fen, player)
                if move and isinstance(move, str):
                    return {"move": move}
            except Exception:
                log.exception("Model prediction failed for FEN=%s", fen)

        # Fallback: random legal move
        moves = self._legal_moves(fen)
        if not moves:
            return {"error": "No legal moves available.", "move": "0000"}

        # Prefer captures (diagonal moves in Breakthrough)
        captures = [m for m in moves if m[0] != m[2]]
        move = random.choice(captures) if captures else random.choice(moves)
        return {"move": move}

    # ──────────────────────────────────────────
    #  Minimal legal-move generator (self-contained)
    # ──────────────────────────────────────────
    @staticmethod
    def _legal_moves(fen: str) -> list[str]:
        """Return all legal UCI moves for the given Breakthrough FEN.

        This is a minimal, self-contained implementation so the
        handler does not depend on the server's breakthrough_engine.
        """
        parts = fen.strip().split()
        turn = parts[1] if len(parts) > 1 else WHITE

        # Parse ranks → grid
        grid: list[list[str]] = []
        for rank_str in parts[0].split("/"):
            row: list[str] = []
            for ch in rank_str:
                if ch.isdigit():
                    row.extend([EMPTY] * int(ch))
                else:
                    row.append(ch)
            grid.append(row)

        piece = WHITE_PIECE if turn == WHITE else BLACK_PIECE
        direction = -1 if turn == WHITE else 1
        moves: list[str] = []

        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if grid[r][c] != piece:
                    continue
                nr = r + direction
                if nr < 0 or nr >= BOARD_SIZE:
                    continue
                # Straight forward on empty
                if grid[nr][c] == EMPTY:
                    moves.append(
                        f"{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - r]}"
                        f"{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                    )
                # Diagonals — capture or empty
                for dc in (-1, 1):
                    nc = c + dc
                    if nc < 0 or nc >= BOARD_SIZE:
                        continue
                    if grid[nr][nc] == piece:
                        continue  # own piece
                    moves.append(
                        f"{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - r]}"
                        f"{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                    )

        return moves
