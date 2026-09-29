"""
chess_mcvs.py — example chess model using the new load()/get_move() contract.

This is a minimal, WORKING example. It doesn't play strong chess — it just
demonstrates the two functions the platform calls and how to read your own
data files. Replace the body of load()/get_move() with your real model.

Contract
--------
  state = load(ctx)                  # called ONCE when your model is loaded.
  move  = get_move(state, fen, player)   # called once per move you're asked for.

``ctx.model_dir`` and ``ctx.data_dir`` are real, read-only folders (Path
objects). ``ctx.data_dir`` is only set if your config_model.json's
"data_files" folder was mounted — see config_model.json in this folder.

The platform does NOT read config_model.json's "zone_db_filename" for models
that define ``load`` — you are responsible for opening any data file you
need yourself, from ``ctx.data_dir``.
"""
from __future__ import annotations

import random

import chess


def load(ctx):
    """Called once. Load your weights / opening book / etc. here.

    Return whatever you want — it's passed back to you as `state` on every
    get_move() call. Below we just demonstrate reading a data file if one
    was declared in config_model.json's "data_files" list.
    """
    state = {"opening_book": None}

    if ctx.data_dir is not None:
        book_path = ctx.data_dir / "opening_book.txt"
        if book_path.exists():
            state["opening_book"] = book_path.read_text(encoding="utf-8").splitlines()

    return state


def get_move(state, fen: str, player: str) -> str:
    """Return a single legal move in UCI format (e.g. "e2e4") for *fen*.

    *player* is "w" or "b" — whoever is to move (matches the FEN's side to
    move, provided for convenience).
    """
    board = chess.Board(fen)
    legal = list(board.legal_moves)
    if not legal:
        # No legal moves — this position is checkmate/stalemate. The
        # platform treats an empty/invalid return as "no move produced".
        return ""

    # Replace this with your real model's move selection.
    return random.choice(legal).uci()
