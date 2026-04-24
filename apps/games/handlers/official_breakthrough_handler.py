"""Official Breakthrough handler — uses repo files but controlled by platform
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def _file_sha256(p: Path) -> str:
    """Return a short SHA-256 hex digest for *p* (first 12 chars)."""
    try:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        return h[:12]
    except Exception:
        return "?"


class EndpointHandler:
    """Official platform Breakthrough handler.

    This handler reads the user's repo directory (path) and imports the
    Python modules listed in `config_model.json` (if any). Unlike user-provided
    `handler.py`, this class is maintained by the platform and will always
    be used for Breakthrough inference.

    Bug-fix notes
    ─────────────
    * Only `.py` files in `config_model.json["files"]` are imported.
      Non-Python files (safetensors, npz, …) were previously silently
      attempted and failed, leaving predict_fn = None.
    * ALL successfully-loaded Python modules are tried for callable
      prediction attributes — not just the one named in `"main"`.
      Repos that omit the `"main"` key now work correctly.
    * When no prediction is possible (predict_fn = None after all
      attempts), __call__ returns ``None`` instead of a hardcoded
      deterministic first-capture move. The caller (runner) falls
      through to try user modules and ultimately the random-legal-move
      fallback in predict_breakthrough.get_move.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.predict_fn = None
        self.zone_db = None
        # Track which modules were loaded for diagnostics
        self._loaded_modules: list[str] = []

        print(f"✅ Using OFFICIAL Breakthrough handler — model_dir={self.path}", file=sys.stderr)
        log.info("✅ Using OFFICIAL Breakthrough handler — model_dir=%s", self.path)

        # ── Log model file inventory with hashes for diagnostics ─────────────
        try:
            for p in sorted(self.path.rglob("*")):
                if p.is_file() and not p.name.startswith(".") and p.name != "_agl_local_runner.py":
                    print(
                        f"  📄 {p.relative_to(self.path)}  sha256={_file_sha256(p)}",
                        file=sys.stderr,
                    )
        except Exception:
            pass

        # Add model dir to sys.path so relative imports work
        if str(self.path) not in sys.path:
            sys.path.insert(0, str(self.path))

        # ── Load config_model.json ────────────────────────────────────────────
        model_config_path = self.path / "config_model.json"
        if model_config_path.exists():
            try:
                with open(model_config_path, encoding="utf-8") as fh:
                    model_config = json.load(fh)
            except Exception:
                model_config = {}
        else:
            model_config = {}

        main_module_name = (model_config.get("main", "") or "").removesuffix(".py")

        # ── Import only Python (.py) files listed in "files" ─────────────────
        # Previously ALL files (including model.safetensors, zone_db.npz …)
        # were passed to spec_from_file_location, which failed silently and
        # left predict_fn = None for every safetensors-only repo.
        py_files = [f for f in model_config.get("files", []) if f.endswith(".py")]
        if not py_files and main_module_name:
            # "main" key may directly name a .py file without listing it
            candidate = self.path / (main_module_name + ".py")
            if candidate.exists():
                py_files = [main_module_name + ".py"]

        for mod_file in py_files:
            mod_name = mod_file.removesuffix(".py")
            mod_path = self.path / mod_file
            if not mod_path.exists():
                print(f"  ⚠️  Python module {mod_file} listed in config but not found", file=sys.stderr)
                log.warning("Module file %s not found — skipping.", mod_file)
                continue
            try:
                spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                self._loaded_modules.append(mod_name)
                print(f"  ✅ Loaded Python module: {mod_file}  sha256={_file_sha256(mod_path)}", file=sys.stderr)
                log.info("Loaded module: %s from %s", mod_name, mod_path)

                # Wire predict_fn from the "main" module or the first module
                # that exposes a recognised prediction interface.
                # Previously only the "main" module was checked — repos without
                # "main" in config always had predict_fn = None.
                is_main = (mod_name == main_module_name) or (not main_module_name)
                if self.predict_fn is None or is_main:
                    self._try_wire_predictor(mod, prefer=is_main)
            except Exception:
                log.exception("Failed importing module %s", mod_file)

        # ── Load zone DB ─────────────────────────────────────────────────────
        # Check config_model.json first (key "zone_db_filename"), then
        # config_data.json (key "zone_db") for backwards compat.
        zone_db_filename = model_config.get("zone_db_filename", "")
        if not zone_db_filename:
            data_config_path = self.path / "config_data.json"
            if data_config_path.exists():
                try:
                    with open(data_config_path, encoding="utf-8") as fh:
                        data_config = json.load(fh)
                    zone_db_filename = data_config.get("zone_db", "") or data_config.get("zone_db_filename", "")
                except Exception:
                    pass

        if zone_db_filename:
            # Prefer DATA_DIR env var (set by runner), then model dir itself
            data_dir_str = os.environ.get("DATA_DIR", "")
            search_dirs = [Path(data_dir_str)] if data_dir_str else []
            search_dirs.append(self.path)
            for base in search_dirs:
                local_zone = base / zone_db_filename
                if local_zone.exists():
                    try:
                        self.zone_db = np.load(str(local_zone), allow_pickle=True)
                        print(f"  ✅ Zone DB loaded: {local_zone}  sha256={_file_sha256(local_zone)}", file=sys.stderr)
                        log.info("Zone DB loaded from %s", local_zone)
                        break
                    except Exception:
                        log.exception("Failed loading zone DB %s", local_zone)

        if self.predict_fn is None:
            print(
                f"  ⚠️  No predict_fn wired — loaded modules: {self._loaded_modules}  "
                f"py_files_in_config: {py_files}  "
                f"returning None on __call__ so runner can try user modules",
                file=sys.stderr,
            )
            log.warning(
                "official_breakthrough_handler: no predict_fn wired for %s "
                "(modules=%s, py_files=%s) — __call__ will return None",
                self.path, self._loaded_modules, py_files,
            )

    def _try_wire_predictor(self, mod, *, prefer: bool = False) -> None:
        """Try to wire predict_fn from *mod*. Only replaces an existing
        predict_fn when *prefer* is True (i.e. this is the "main" module)."""
        if self.predict_fn is not None and not prefer:
            return

        if hasattr(mod, "get_move"):
            raw_fn = mod.get_move
            self.predict_fn = lambda fen, player: raw_fn(fen, player, zone_db=self.zone_db)
            print(f"  ✅ Handler wired: {mod.__name__}.get_move()", file=sys.stderr)
            log.info("Using %s.get_move() for predictions.", mod.__name__)
            return

        if hasattr(mod, "UCTSearcher"):
            searcher_cls = mod.UCTSearcher
            kwargs = {} if self.zone_db is None else {"zone_db": self.zone_db}
            searcher = searcher_cls(**kwargs)
            self.predict_fn = lambda fen, player: searcher.search(fen, player)
            print(f"  ✅ Handler wired: {mod.__name__}.UCTSearcher", file=sys.stderr)
            log.info("Using %s.UCTSearcher for predictions.", mod.__name__)
            return

        if hasattr(mod, "predict"):
            raw_fn = mod.predict
            self.predict_fn = lambda fen, player: raw_fn(fen, player)
            print(f"  ✅ Handler wired: {mod.__name__}.predict()", file=sys.stderr)
            log.info("Using %s.predict() for predictions.", mod.__name__)
            return

    def __call__(self, data: dict) -> None | dict:
        """Return ``{"move": "<uci>"}`` or ``None``.

        Returning ``None`` signals the runner that no prediction was made,
        so it can fall through to user modules or the random-legal-move
        fallback in predict_breakthrough.get_move.  Previously this method
        returned a hardcoded ``captures[0]`` move when no model was loaded,
        causing ALL repos to produce identical moves for the same position.
        """
        inputs = data.get("inputs", data)
        fen = inputs.get("fen", "")
        player = inputs.get("player", "w")

        if not fen:
            return {"error": "Missing 'fen' in request."}

        if self.predict_fn is None:
            log.warning(
                "official_breakthrough_handler: predict_fn is None for %s — returning None",
                self.path,
            )
            return None

        try:
            move = self.predict_fn(fen, player)
            if move and isinstance(move, str):
                print(f"  ✅ Handler raw move: {move!r}  player={player}", file=sys.stderr)
                log.info(
                    "official_breakthrough_handler: move=%r player=%s model=%s",
                    move, player, self.path,
                )
                return {"move": move}
            log.warning(
                "official_breakthrough_handler: predict_fn returned non-string %r — returning None",
                move,
            )
            return None
        except Exception:
            log.exception(
                "official_breakthrough_handler: prediction failed for FEN=%s model=%s",
                fen, self.path,
            )
            return None

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
