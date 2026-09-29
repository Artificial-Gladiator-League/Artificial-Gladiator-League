#!/usr/bin/env python3
"""
check_my_model.py — standalone, no-Django, no-Docker local checker.

Run this on your own machine BEFORE uploading, to catch problems early:

    pip install python-chess
    python check_my_model.py /path/to/your/model_dir [--data-dir /path/to/data]

It mirrors (a subset of) apps.games.model_check.check_model():
  1. UTF-8/syntax check of your entrypoint file(s) (compiled, not executed,
     for this first pass).
  2. config_model.json field validation.
  3. Imports your model IN THIS PROCESS (unlike the platform, which always
     runs your code in an isolated Docker sandbox) and calls load() +
     get_move() for 3 sample positions, checking every returned move is a
     legal UCI move.

This script intentionally has NO Django/platform dependencies so you can
run it standalone. It is NOT a substitute for the platform's own sandboxed
check — it's a fast, local way to catch mistakes before you upload.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

_ENCODINGS = ("utf-8-sig", "cp1255", "cp1252", "latin-1")


def _decode_bytes(data: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _load_manifest(model_dir: Path) -> tuple[dict, list[str]]:
    cfg_path = model_dir / "config_model.json"
    if not cfg_path.exists():
        return {}, []
    try:
        manifest = json.loads(_decode_bytes(cfg_path.read_bytes()))
    except Exception as exc:
        return {}, [f"config_model.json is not valid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return {}, ["config_model.json must contain a JSON object"]
    return manifest, []


def _check_manifest_fields(manifest: dict, model_dir: Path, data_dir: Path | None) -> list[str]:
    problems = []
    if "schema_version" in manifest and not isinstance(manifest["schema_version"], int):
        problems.append("manifest field 'schema_version' must be an integer")
    for field in ("modules", "data_files", "requirements"):
        if field in manifest and not isinstance(manifest[field], list):
            problems.append(f"manifest field '{field}' must be a list of strings")
    entrypoint = manifest.get("entrypoint")
    if entrypoint is not None:
        if not isinstance(entrypoint, str):
            problems.append("manifest field 'entrypoint' must be a string")
        elif not (model_dir / entrypoint).exists():
            problems.append(f"entrypoint '{entrypoint}' does not exist")
    for fname in manifest.get("data_files") or []:
        if not isinstance(fname, str):
            continue
        if data_dir is None or not (data_dir / fname).exists():
            problems.append(f"data file '{fname}' listed in config_model.json was not found")
    return problems


def _entrypoint_files(manifest: dict, model_dir: Path, game_type: str) -> list[Path]:
    modules = manifest.get("modules")
    if modules:
        names = [m for m in modules if isinstance(m, str)]
    else:
        entrypoint = manifest.get("entrypoint")
        if isinstance(entrypoint, str):
            names = [entrypoint]
        else:
            default = f"{game_type or 'chess'}_mcvs.py"
            names = [default] if (model_dir / default).exists() else [
                p.name for p in sorted(model_dir.glob("*.py")) if not p.name.startswith("_agl_")
            ]
    return [model_dir / n for n in names]


def _check_syntax(files: list[Path]) -> list[str]:
    problems = []
    existing = [f for f in files if f.exists()]
    if not existing:
        problems.append("no .py model file found")
        return problems
    for f in existing:
        text = _decode_bytes(f.read_bytes())
        try:
            compile(text, f.name, "exec", dont_inherit=True)
        except SyntaxError as exc:
            problems.append(f"'{f.name}' has a syntax error: {exc}")
    return problems


def _import_module(files: list[Path]):
    module = None
    for f in files:
        if not f.exists():
            continue
        mod_name = f.stem
        spec = importlib.util.spec_from_file_location(mod_name, str(f))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        module = mod
    return module


class _Context:
    def __init__(self, model_dir, data_dir, game_type):
        self.model_dir = model_dir
        self.data_dir = data_dir
        self.game_type = game_type


def _chess_sample_positions():
    return [
        ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "w"),
        ("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3", "w"),
        ("8/8/8/3k4/8/3K4/8/7R w - - 0 1", "w"),
    ]


def _chess_is_legal(fen, uci):
    import chess
    board = chess.Board(fen)
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return False
    return move in board.legal_moves


_BT_FILES = "abcdefgh"
_BT_RANKS = "12345678"


def _breakthrough_sample_positions():
    return [
        ("BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w", "w"),
        ("BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW b", "b"),
        ("8/8/2B1B3/8/3W4/8/8/8 w", "w"),
    ]


def _breakthrough_grid(fen):
    ranks = fen.strip().split()[0].split("/")
    grid = []
    for rank_str in ranks:
        row = []
        for ch in rank_str:
            row.extend(["."] * int(ch)) if ch.isdigit() else row.append(ch)
        grid.append(row)
    return grid


def _breakthrough_legal_moves(fen):
    grid = _breakthrough_grid(fen)
    parts = fen.strip().split()
    turn = parts[1] if len(parts) > 1 else "w"
    piece = "W" if turn == "w" else "B"
    direction = -1 if turn == "w" else 1
    moves = []
    for r in range(8):
        for c in range(8):
            if grid[r][c] != piece:
                continue
            nr = r + direction
            if nr < 0 or nr >= 8:
                continue
            if grid[nr][c] == ".":
                moves.append(_BT_FILES[c] + _BT_RANKS[7 - r] + _BT_FILES[c] + _BT_RANKS[7 - nr])
            for dc in (-1, 1):
                nc = c + dc
                if nc < 0 or nc >= 8:
                    continue
                if grid[nr][nc] != piece:
                    moves.append(_BT_FILES[c] + _BT_RANKS[7 - r] + _BT_FILES[nc] + _BT_RANKS[7 - nr])
    return moves


def _breakthrough_is_legal(fen, uci):
    return isinstance(uci, str) and len(uci) == 4 and uci in _breakthrough_legal_moves(fen)


def _sample_positions(game_type):
    return _breakthrough_sample_positions() if game_type == "breakthrough" else _chess_sample_positions()


def _is_legal(game_type, fen, uci):
    return _breakthrough_is_legal(fen, uci) if game_type == "breakthrough" else _chess_is_legal(fen, uci)


def check_model(model_dir: Path, data_dir: Path | None, game_type: str) -> list[str]:
    manifest, problems = _load_manifest(model_dir)
    problems += _check_manifest_fields(manifest, model_dir, data_dir)

    files = _entrypoint_files(manifest, model_dir, game_type)
    problems += _check_syntax(files)
    if problems:
        return problems

    try:
        module = _import_module(files)
    except Exception as exc:
        return [f"module import failed: {exc}"]
    if module is None:
        return ["no model module found"]

    is_new_style = hasattr(module, "load")
    state = None
    if is_new_style:
        try:
            state = module.load(_Context(model_dir, data_dir, game_type))
        except Exception as exc:
            return [f"load(ctx) raised an exception: {exc}"]
    elif not (hasattr(module, "get_move") or hasattr(module, "UCTSearcher") or hasattr(module, "predict")):
        return ["module has no load/get_move, get_move, UCTSearcher, or predict entrypoint"]

    for i, (fen, player) in enumerate(_sample_positions(game_type), start=1):
        try:
            if is_new_style:
                move = module.get_move(state, fen, player)
            elif hasattr(module, "get_move"):
                move = module.get_move(fen, player, zone_db=None)
            elif hasattr(module, "UCTSearcher"):
                move = module.UCTSearcher().search(fen, player)
            else:
                move = module.predict(fen, player)
        except Exception as exc:
            problems.append(f"sample position {i} raised an exception: {exc}")
            continue
        if not move or not isinstance(move, str):
            problems.append(f"sample position {i}: model returned no move")
            continue
        if not _is_legal(game_type, fen, move):
            problems.append(f"sample position {i}: '{move}' is not a legal move for this position")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path, help="Path to your model directory")
    parser.add_argument("--data-dir", type=Path, default=None, help="Path to your data directory (optional)")
    parser.add_argument("--game-type", type=str, default=None, help="Override game_type (default: read from config_model.json, else 'chess')")
    args = parser.parse_args()

    model_dir = args.model_dir.resolve()
    data_dir = args.data_dir.resolve() if args.data_dir else None

    manifest, _ = _load_manifest(model_dir)
    game_type = args.game_type or manifest.get("game_type") or "chess"

    problems = check_model(model_dir, data_dir, game_type)

    if not problems:
        print("OK — no problems found.")
        return 0

    print(f"{len(problems)} problem(s) found:")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
