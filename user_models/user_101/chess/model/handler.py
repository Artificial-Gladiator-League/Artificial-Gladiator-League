# ──────────────────────────────────────────────
# handler.py
#
# Custom handler for HF Inference Endpoints.
#
# When HF Inference Endpoints detects a handler.py
# in the repo root, it uses this class to serve
# predictions instead of the default Transformers
# pipeline.
#
# Expected repo layout:
#
#   your-username/chess-model/
#   ├── handler.py              ← this file
#   ├── config_model.json       ← lists your Python modules + main
#   ├── config_data.json        ← lists data files + zone_db key
#   ├── chess_mcvs.py    ← your UCT search / eval module
#   └── requirements.txt        ← runtime deps (numpy, huggingface_hub)
#
# config_model.json example:
#
#   {
#       "files": ["matrix_model.py", "abc_model.py", "chess_mcvs.py"],
#       "main": "chess_mcvs"
#   }
#
# config_data.json example:
#
#   {
#       "files": ["chess_zone_db.npz"],
#       "zone_db": "chess_zone_db.npz"
#   }
#
# Set the HF_DATA_REPO_ID environment variable to
# the HF dataset repo holding your data files.
#
# Request format (sent by Agladiator):
#
#   POST /
#   {
#       "inputs": {
#           "fen": "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w",
#           "player": "w",
#           "game_type": "chess"
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
#  Chess board constants (used by the minimal legal-move generator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOARD_SIZE = 8
_FILES = "abcdefgh"
_RANKS = "12345678"
WHITE, BLACK = "w", "b"
EMPTY = "."


class EndpointHandler:
    """Custom handler for chess AI on HF Inference Endpoints.

    Lifecycle:
      1. ``__init__`` is called once when the endpoint container starts.
         Use it to load your model artifacts (modules, zone DB, etc.).
      2. ``__call__`` is called for every inference request.
         It receives the parsed JSON body and must return a dict.
    """

    def __init__(self, path: str):
        """Load model artefacts from *path* (the cloned repo directory).

        Steps:
          1. Read ``config_model.json`` to discover module files and main module.
          2. Dynamically import each listed module.
          3. Read ``config_data.json``, download data files, load zone DB.
          4. Initialise the search engine.
        """
        self.path = Path(path)
        self.predict_fn = None
        self.zone_db = None
        self.module = None

        # ── 1. Read config_model.json ───────────
        model_config_path = self.path / "config_model.json"
        if not model_config_path.exists():
            log.warning("No config_model.json found in %s", path)
            model_config: dict = {}
        else:
            with open(model_config_path) as fh:
                model_config = json.load(fh)

        main_module_name = model_config.get("main", "")

        # ── 2. Import listed modules ────────────
        # Add repo dir to sys.path so relative imports work.
        if str(self.path) not in sys.path:
            sys.path.insert(0, str(self.path))

        for mod_file in model_config.get("files", []):
            mod_name = mod_file.removesuffix(".py")
            mod_path = self.path / mod_file
            if not mod_path.exists():
                log.warning("Module file %s not found — skipping.", mod_file)
                continue
            spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            log.info("Loaded module: %s", mod_name)
            # Mark the module matching "main" as the primary module.
            if mod_name == main_module_name:
                self.module = mod

        # ── 3. Read config_data.json & load zone DB ──
        data_config_path = self.path / "config_data.json"
        if not data_config_path.exists():
            log.warning("No config_data.json found in %s", path)
            data_config: dict = {}
        else:
            with open(data_config_path) as fh:
                data_config = json.load(fh)

        data_repo = os.environ.get("HF_DATA_REPO_ID", "")
        downloaded_files: dict[str, str] = {}

        if data_repo:
            try:
                from huggingface_hub import hf_hub_download
            except Exception:
                hf_hub_download = None
                log.warning(
                    "huggingface_hub not installed — cannot download data files. Add it to requirements.txt"
                )

            if hf_hub_download is not None:
                for data_file in data_config.get("files", []):
                    try:
                        local_path = hf_hub_download(
                            repo_id=data_repo,
                            filename=data_file,
                            repo_type="dataset",
                        )
                        downloaded_files[data_file] = local_path
                        log.info("Downloaded %s from %s", data_file, data_repo)
                    except Exception:
                        log.warning(
                            "Failed to download %s from %s — skipping.",
                            data_file,
                            data_repo,
                        )
        else:
            log.warning(
                "HF_DATA_REPO_ID not set — skipping data file downloads."
            )

        zone_db_filename = data_config.get("zone_db", "")
        if zone_db_filename and zone_db_filename in downloaded_files:
            self.zone_db = np.load(
                downloaded_files[zone_db_filename], allow_pickle=True
            )
            log.info("Zone DB loaded from data repo %s", data_repo)
        elif zone_db_filename:
            local_zone = self.path / zone_db_filename
            if local_zone.exists():
                self.zone_db = np.load(str(local_zone), allow_pickle=True)
                log.info("Zone DB loaded from local file %s", local_zone)
            else:
                log.warning("No zone DB found for '%s'.", zone_db_filename)

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

        # Prefer captures (diagonal moves in chess)
        captures = [m for m in moves if m[0] != m[2]]
        move = random.choice(captures) if captures else random.choice(moves)
        return {"move": move}

    # ──────────────────────────────────────────
    #  Minimal legal-move generator (self-contained)
    # ──────────────────────────────────────────
    @staticmethod
    def _legal_moves(fen: str) -> list[str]:
        """Return a list of pseudo-legal UCI moves for a chess FEN.

        This minimal generator supports pawn moves (including promotions
        and en-passant), knights, bishops, rooks, queens, kings and
        basic castling availability. It does not perform check detection
        and therefore may return some pseudo-legal moves in checked positions.
        """
        parts = fen.strip().split()
        board_part = parts[0] if parts else ""
        turn = parts[1] if len(parts) > 1 else WHITE
        castling = parts[2] if len(parts) > 2 else "-"
        ep_square = parts[3] if len(parts) > 3 else "-"

        # Parse FEN → grid (8 ranks, top (rank 8) → bottom (rank 1))
        grid: list[list[str]] = []
        for rank_str in board_part.split("/"):
            row: list[str] = []
            for ch in rank_str:
                if ch.isdigit():
                    row.extend([EMPTY] * int(ch))
                else:
                    row.append(ch)
            # normalize row length
            if len(row) != BOARD_SIZE:
                row = (row + [EMPTY] * BOARD_SIZE)[:BOARD_SIZE]
            grid.append(row)
        while len(grid) < BOARD_SIZE:
            grid.append([EMPTY] * BOARD_SIZE)

        side_white = turn == WHITE

        def is_enemy(ch: str) -> bool:
            return ch != EMPTY and ch.isupper() != side_white

        def is_own(ch: str) -> bool:
            return ch != EMPTY and ch.isupper() == side_white

        moves: list[str] = []

        # En-passant target square (if any)
        ep_r = ep_c = None
        if ep_square and ep_square != "-":
            try:
                ep_c = _FILES.index(ep_square[0])
                ep_r = BOARD_SIZE - int(ep_square[1])
            except Exception:
                ep_r = ep_c = None

        start_row = 6 if side_white else 1
        last_row = 0 if side_white else 7
        direction = -1 if side_white else 1

        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                ch = grid[r][c]
                if ch == EMPTY:
                    continue
                if ch.isupper() != side_white:
                    continue
                piece = ch.lower()
                from_sq = f"{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - r]}"

                if piece == "p":
                    nr = r + direction
                    # forward one
                    if 0 <= nr < BOARD_SIZE and grid[nr][c] == EMPTY:
                        # promotion
                        if nr == last_row:
                            for promo in ("q", "r", "b", "n"):
                                moves.append(
                                    f"{from_sq}{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - nr]}{promo}"
                                )
                        else:
                            moves.append(
                                f"{from_sq}{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                            )
                            # double move from start
                            if r == start_row:
                                nr2 = r + 2 * direction
                                if 0 <= nr2 < BOARD_SIZE and grid[nr2][c] == EMPTY:
                                    moves.append(
                                        f"{from_sq}{_FILES[c]}{_RANKS[BOARD_SIZE - 1 - nr2]}"
                                    )
                    # captures (including en-passant)
                    for dc in (-1, 1):
                        nc = c + dc
                        if nc < 0 or nc >= BOARD_SIZE:
                            continue
                        if 0 <= nr < BOARD_SIZE:
                            target = grid[nr][nc]
                            if is_enemy(target):
                                if nr == last_row:
                                    for promo in ("q", "r", "b", "n"):
                                        moves.append(
                                            f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}{promo}"
                                        )
                                else:
                                    moves.append(
                                        f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                                    )
                            # en-passant capture
                            if ep_r is not None and ep_r == nr and ep_c == nc:
                                cap_r = r
                                cap_c = nc
                                if (
                                    0 <= cap_r < BOARD_SIZE
                                    and grid[cap_r][cap_c].lower() == "p"
                                    and is_enemy(grid[cap_r][cap_c])
                                ):
                                    moves.append(
                                        f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                                    )

                elif piece == "n":
                    for dr, dc in (
                        (-2, -1),
                        (-2, 1),
                        (-1, -2),
                        (-1, 2),
                        (1, -2),
                        (1, 2),
                        (2, -1),
                        (2, 1),
                    ):
                        nr = r + dr
                        nc = c + dc
                        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
                            target = grid[nr][nc]
                            if not is_own(target):
                                moves.append(
                                    f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                                )

                elif piece in ("b", "r", "q"):
                    directions = []
                    if piece in ("b", "q"):
                        directions += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
                    if piece in ("r", "q"):
                        directions += [(-1, 0), (1, 0), (0, -1), (0, 1)]
                    for dr, dc in directions:
                        nr = r + dr
                        nc = c + dc
                        while 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
                            target = grid[nr][nc]
                            if target == EMPTY:
                                moves.append(
                                    f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                                )
                            else:
                                if is_enemy(target):
                                    moves.append(
                                        f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                                    )
                                break
                            nr += dr
                            nc += dc

                elif piece == "k":
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            if dr == 0 and dc == 0:
                                continue
                            nr = r + dr
                            nc = c + dc
                            if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
                                target = grid[nr][nc]
                                if not is_own(target):
                                    moves.append(
                                        f"{from_sq}{_FILES[nc]}{_RANKS[BOARD_SIZE - 1 - nr]}"
                                    )
                    # Basic castling checks (does not verify check conditions)
                    if side_white:
                        if "K" in castling and r == 7 and c == 4:
                            if grid[7][5] == EMPTY and grid[7][6] == EMPTY and grid[7][7] != EMPTY and grid[7][7].isupper():
                                moves.append(f"{from_sq}g1")
                        if "Q" in castling and r == 7 and c == 4:
                            if (
                                grid[7][3] == EMPTY
                                and grid[7][2] == EMPTY
                                and grid[7][1] == EMPTY
                                and grid[7][0] != EMPTY
                                and grid[7][0].isupper()
                            ):
                                moves.append(f"{from_sq}c1")
                    else:
                        if "k" in castling and r == 0 and c == 4:
                            if grid[0][5] == EMPTY and grid[0][6] == EMPTY and grid[0][7] != EMPTY and grid[0][7].islower():
                                moves.append(f"{from_sq}g8")
                        if "q" in castling and r == 0 and c == 4:
                            if (
                                grid[0][3] == EMPTY
                                and grid[0][2] == EMPTY
                                and grid[0][1] == EMPTY
                                and grid[0][0] != EMPTY
                                and grid[0][0].islower()
                            ):
                                moves.append(f"{from_sq}c8")

        return moves
