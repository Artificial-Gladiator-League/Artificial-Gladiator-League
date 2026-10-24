"""Breakthrough (8x8) rules for ShiRi, mirroring apps/games/breakthrough_engine.py.

Board state: the site's FEN-like string (``"BBBBBBBB/.../WWWWWWWW w"``), rank 8 first,
or an ``(grid, turn)`` pair where grid[row][col] is 'W' / 'B' / '.'.
Moves: ``"a2a3"``-style strings.  Internally a move is ``(from_idx, to_idx)`` with
``idx = row * 8 + col`` and row 0 = rank 8, exactly like the site's grid.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from apps.shiri.constants import BLACK, DRAW, WHITE
from apps.shiri.rules.base import GameRules, GameState
from apps.shiri.util.rewards import Rewards

_FILES = "abcdefgh"
STARTING_FEN = "BBBBBBBB/BBBBBBBB/8/8/8/8/WWWWWWWW/WWWWWWWW w"
_W, _B = 1, 2


def _sq_name(idx: int) -> str:
    return f"{_FILES[idx % 8]}{8 - idx // 8}"


def _sq_idx(name: str) -> int:
    return (8 - int(name[1])) * 8 + _FILES.index(name[0])


def parse_fen(fen: str) -> tuple[list[int], int]:
    parts = fen.strip().split()
    if not parts:
        raise ValueError("Empty Breakthrough FEN")
    turn = BLACK if len(parts) > 1 and parts[1] == "b" else WHITE
    ranks = parts[0].split("/")
    cells: list[int] = []
    if len(ranks) != 8:
        raise ValueError(f"Invalid Breakthrough FEN: {fen!r}")
    for rank in ranks:
        row: list[int] = []
        for ch in rank:
            if ch.isdigit():
                row.extend([0] * int(ch))
            elif ch == "W":
                row.append(_W)
            elif ch == "B":
                row.append(_B)
            else:
                raise ValueError(f"Invalid Breakthrough FEN: {fen!r}")
        if len(row) != 8:
            raise ValueError(f"Invalid Breakthrough FEN: {fen!r}")
        cells.extend(row)
    return cells, turn


class BreakthroughState(GameState):
    __slots__ = ("cells", "_turn", "_ply", "_moves", "_result", "_result_ready")

    def __init__(self, cells: list[int], turn: int, ply: int, rules: "BreakthroughRules"):
        self.cells = cells
        self._turn = turn
        self._ply = ply
        self.rules = rules
        self._moves = None
        self._result = None
        self._result_ready = False

    def turn(self) -> int:
        return self._turn

    def ply(self) -> int:
        return self._ply

    def _targets(self, idx: int, color: int):
        cells = self.cells
        d = -1 if color == _W else 1
        r, c = divmod(idx, 8)
        nr = r + d
        if nr < 0 or nr > 7:
            return
        base = nr * 8
        if cells[base + c] == 0:
            yield base + c
        if c > 0 and cells[base + c - 1] != color:
            yield base + c - 1
        if c < 7 and cells[base + c + 1] != color:
            yield base + c + 1

    def legal_moves(self) -> list:
        if self._moves is None:
            me = _W if self._turn == WHITE else _B
            moves = []
            for idx, v in enumerate(self.cells):
                if v == me:
                    for t in self._targets(idx, me):
                        moves.append((idx, t))
            self._moves = moves
        return self._moves

    def push(self, move) -> None:
        f, t = move
        self.cells[t] = self.cells[f]
        self.cells[f] = 0
        self._turn = 1 - self._turn
        self._ply += 1
        self._moves = None
        self._result_ready = False

    def copy(self) -> "BreakthroughState":
        return BreakthroughState(self.cells[:], self._turn, self._ply, self.rules)

    def result(self) -> Optional[int]:
        if self._result_ready:
            return self._result
        cells = self.cells
        res = None
        if _W in cells[0:8]:
            res = WHITE
        elif _B in cells[56:64]:
            res = BLACK
        elif _W not in cells:
            res = BLACK
        elif _B not in cells:
            res = WHITE
        elif not self.legal_moves():
            res = 1 - self._turn  # the stuck side loses
        self._result, self._result_ready = res, True
        return res

    def move_uci(self, move) -> str:
        return _sq_name(move[0]) + _sq_name(move[1])

    def parse_uci(self, uci: str):
        if not isinstance(uci, str) or len(uci) != 4:
            return None
        try:
            move = (_sq_idx(uci[:2]), _sq_idx(uci[2:]))
        except (ValueError, IndexError):
            return None
        return move if move in self.legal_moves() else None

    @staticmethod
    def _index(idx: int, perspective: int) -> int:
        return idx if perspective == WHITE else idx ^ 56

    def tokens(self, perspective: int) -> np.ndarray:
        arr = np.array(self.cells, dtype=np.int8)
        me = _W if perspective == WHITE else _B
        out = np.zeros(64, dtype=np.int8)
        out[arr == me] = 1
        out[(arr != 0) & (arr != me)] = 2
        return out if perspective == WHITE else out.reshape(8, 8)[::-1].reshape(64).copy()

    def connectivity(self, perspective: int) -> np.ndarray:
        c = np.zeros((64, 64), dtype=np.float32)
        for idx, v in enumerate(self.cells):
            if v:
                i = self._index(idx, perspective)
                for t in self._targets(idx, v):
                    c[i, self._index(t, perspective)] = 1.0
        return c

    def fen(self) -> str:
        rows = []
        for r in range(8):
            out, empty = "", 0
            for v in self.cells[r * 8:(r + 1) * 8]:
                if v == 0:
                    empty += 1
                else:
                    if empty:
                        out += str(empty)
                        empty = 0
                    out += "W" if v == _W else "B"
            rows.append(out + (str(empty) if empty else ""))
        return "/".join(rows) + (" w" if self._turn == WHITE else " b")

    def material_score(self, perspective: int) -> float:
        me = _W if perspective == WHITE else _B
        own = opp = adv_own = adv_opp = 0
        lead_own = lead_opp = 0
        for idx, v in enumerate(self.cells):
            if not v:
                continue
            r = idx // 8
            adv = (7 - r) if v == _W else r
            if v == me:
                own += 1
                adv_own += adv
                lead_own = max(lead_own, adv)
            else:
                opp += 1
                adv_opp += adv
                lead_opp = max(lead_opp, adv)
        return Rewards.squash(
            0.25 * (own - opp) + (adv_own - adv_opp) / 30.0 + 0.3 * (lead_own - lead_opp), 1.0
        )

    def playout_move(self, rng):
        moves = self.legal_moves()
        cells = self.cells
        me = _W if self._turn == WHITE else _B
        goal_row = 0 if me == _W else 7
        for m in moves:
            if m[1] // 8 == goal_row:
                return m
        opp = _B if me == _W else _W
        threat_row = 6 if me == _W else 1  # opponent pieces one step from their goal
        threats = {i for i in range(threat_row * 8, threat_row * 8 + 8) if cells[i] == opp}
        if threats:
            stops = [m for m in moves if m[1] in threats]
            if stops:
                return rng.choice(stops)
        if rng.random() < 0.6:
            caps = [m for m in moves if cells[m[1]] != 0]
            if caps:
                return rng.choice(caps)
        return rng.choice(moves)

    def board_state(self):
        return self.fen()


class BreakthroughRules(GameRules):
    name = "breakthrough"
    n_tokens = 2
    token_weights = np.array([1.0, -1.0], dtype=np.float32)
    playout_max_plies = 120
    background_max_plies = 200

    def initial_state(self) -> BreakthroughState:
        return self.state_from(STARTING_FEN, ply=0)

    def state_from(self, board_state, ply: Optional[int] = None) -> BreakthroughState:
        if isinstance(board_state, BreakthroughState):
            return BreakthroughState(board_state.cells[:], board_state.turn(), board_state.ply(), self)
        if isinstance(board_state, str):
            cells, turn = parse_fen(board_state)
        elif isinstance(board_state, (tuple, list)) and len(board_state) == 2 and isinstance(board_state[0], list):
            grid, t = board_state
            cells = [{"W": _W, "B": _B}.get(ch, 0) for row in grid for ch in row]
            if len(cells) != 64:
                raise ValueError("Breakthrough grid must be 8x8")
            turn = BLACK if t == "b" else WHITE
        else:
            raise TypeError(f"Unsupported breakthrough board_state: {type(board_state).__name__}")
        return BreakthroughState(cells, turn, self._estimate_ply(cells) if ply is None else ply, self)

    @staticmethod
    def _estimate_ply(cells: list[int]) -> int:
        """The FEN has no move counter: derive a rough ply from advancement and captures."""
        adv = sum((7 - i // 8) if v == _W else (i // 8) for i, v in enumerate(cells) if v)
        pieces = sum(1 for v in cells if v)
        return max(0, adv - 16) + (32 - pieces) * 2
