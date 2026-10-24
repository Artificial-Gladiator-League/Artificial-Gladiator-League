"""Chess rules for ShiRi, backed by python-chess (the same library the site uses).

Board state: a ``chess.Board`` or the FEN string stored in ``Game.current_fen``.
Moves: UCI strings, e.g. ``e2e4`` / ``e7e8q`` (the site's ``move_list`` format).
"""
from __future__ import annotations

from typing import Optional

import chess
import numpy as np

from apps.shiri.constants import BLACK, DRAW, WHITE
from apps.shiri.rules.base import GameRules, GameState
from apps.shiri.util.rewards import Rewards

_VALUES = {
    chess.PAWN: 1.0, chess.KNIGHT: 3.0, chess.BISHOP: 3.2,
    chess.ROOK: 5.0, chess.QUEEN: 9.0, chess.KING: 2.0,
}
_MATERIAL_VALUES = {**_VALUES, chess.KING: 0.0}


class ChessState(GameState):
    __slots__ = ("board", "_moves")

    def __init__(self, board: chess.Board, rules: "ChessRules"):
        self.board = board
        self.rules = rules
        self._moves = None

    def turn(self) -> int:
        return WHITE if self.board.turn else BLACK

    def ply(self) -> int:
        return self.board.ply()

    def legal_moves(self) -> list:
        if self._moves is None:
            self._moves = list(self.board.legal_moves)
        return self._moves

    def push(self, move) -> None:
        self.board.push(move)
        self._moves = None

    def copy(self) -> "ChessState":
        return ChessState(self.board.copy(stack=False), self.rules)

    def result(self) -> Optional[int]:
        if not self.legal_moves():
            return (BLACK if self.board.turn else WHITE) if self.board.is_check() else DRAW
        if self.board.halfmove_clock >= 100 or self.board.is_insufficient_material():
            return DRAW
        return None

    def full_result(self) -> Optional[int]:
        r = self.result()
        if r is not None:
            return r
        if self.board.is_fivefold_repetition() or self.board.can_claim_threefold_repetition():
            return DRAW
        return None

    def outcomes_after_moves(self) -> dict:
        out = {}
        for m in self.legal_moves():
            b = self.board.copy()  # keeps the stack so repetition draws are seen
            b.push(m)
            r = ChessState(b, self.rules).full_result()
            if r is not None:
                out[m] = r
        return out

    def move_uci(self, move) -> str:
        return move.uci()

    def parse_uci(self, uci: str):
        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, chess.InvalidMoveError):
            return None
        legal = self.legal_moves()
        if move in legal:
            return move
        if move.promotion is None:  # the site auto-queens
            queen = chess.Move(move.from_square, move.to_square, chess.QUEEN)
            if queen in legal:
                return queen
        return None

    @staticmethod
    def _index(sq: int, perspective: int) -> int:
        return sq if perspective == WHITE else sq ^ 56

    def tokens(self, perspective: int) -> np.ndarray:
        out = np.zeros(64, dtype=np.int8)
        own_color = perspective == WHITE
        for sq, piece in self.board.piece_map().items():
            tok = piece.piece_type + (0 if piece.color == own_color else 6)
            out[self._index(sq, perspective)] = tok
        return out

    def connectivity(self, perspective: int) -> np.ndarray:
        board = self.board
        src, dst = [], []
        for sq in board.piece_map():
            for target in board.attacks(sq):
                src.append(sq)
                dst.append(target)
        c = np.zeros((64, 64), dtype=np.float32)
        if src:
            s, d = np.array(src), np.array(dst)
            if perspective != WHITE:
                s, d = s ^ 56, d ^ 56
            c[s, d] = 1.0
        return c

    def fen(self) -> str:
        return self.board.fen()

    def material_score(self, perspective: int) -> float:
        own_color = perspective == WHITE
        diff = 0.0
        for piece in self.board.piece_map().values():
            v = _MATERIAL_VALUES[piece.piece_type]
            diff += v if piece.color == own_color else -v
        return Rewards.squash(diff, 6.0)

    def playout_move(self, rng):
        moves = self.legal_moves()
        if rng.random() < 0.7:
            board = self.board
            forcing = [m for m in moves if m.promotion or board.is_capture(m)]
            if forcing:
                return rng.choice(forcing)
        return rng.choice(moves)

    def board_state(self):
        return self.board


class ChessRules(GameRules):
    name = "chess"
    n_tokens = 12
    # own pieces positive, opponent pieces negative, so mirrored material cancels out
    token_weights = np.array(
        [_VALUES[t] for t in range(1, 7)] + [-_VALUES[t] for t in range(1, 7)], dtype=np.float32
    )
    playout_max_plies = 40
    background_max_plies = 120

    def initial_state(self) -> ChessState:
        return ChessState(chess.Board(), self)

    def state_from(self, board_state, ply: Optional[int] = None) -> ChessState:
        if isinstance(board_state, ChessState):
            return ChessState(board_state.board.copy(), self)
        if isinstance(board_state, chess.Board):
            return ChessState(board_state.copy(), self)
        if isinstance(board_state, str):
            try:
                return ChessState(chess.Board(board_state.strip()), self)
            except ValueError as exc:
                raise ValueError(f"Invalid chess FEN: {board_state!r}") from exc
        raise TypeError(f"Unsupported chess board_state: {type(board_state).__name__}")
