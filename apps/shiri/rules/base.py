"""Game-state / rules interfaces the MCVS search is written against."""
from __future__ import annotations

import abc
from typing import Optional

import numpy as np


class GameState(abc.ABC):
    """A mutable position. Moves are opaque objects; ``move_uci`` gives the site's wire format."""

    rules: "GameRules"

    @abc.abstractmethod
    def turn(self) -> int: ...

    @abc.abstractmethod
    def ply(self) -> int: ...

    @abc.abstractmethod
    def legal_moves(self) -> list: ...

    @abc.abstractmethod
    def push(self, move) -> None: ...

    @abc.abstractmethod
    def copy(self) -> "GameState":
        """Cheap copy for search (no history)."""

    @abc.abstractmethod
    def result(self) -> Optional[int]:
        """WHITE / BLACK / DRAW once the game is over, else None (cheap checks only)."""

    @abc.abstractmethod
    def move_uci(self, move) -> str: ...

    @abc.abstractmethod
    def parse_uci(self, uci: str):
        """Return the legal move matching ``uci`` or None."""

    @abc.abstractmethod
    def tokens(self, perspective: int) -> np.ndarray: ...

    @abc.abstractmethod
    def connectivity(self, perspective: int) -> np.ndarray: ...

    @abc.abstractmethod
    def fen(self) -> str: ...

    @abc.abstractmethod
    def material_score(self, perspective: int) -> float:
        """Heuristic advantage of ``perspective`` in [-1, 1]."""

    @abc.abstractmethod
    def playout_move(self, rng): ...

    @abc.abstractmethod
    def board_state(self):
        """The site-native board object/string for this position."""

    def outcomes_after_moves(self) -> dict:
        """move -> result for every legal move that ends the game (full rules, incl. repetition)."""
        out = {}
        for m in self.legal_moves():
            child = self.copy()
            child.push(m)
            r = child.result()
            if r is not None:
                out[m] = r
        return out

    def full_result(self) -> Optional[int]:
        """Like ``result`` but with every rule the site enforces (e.g. claimable draws)."""
        return self.result()


class GameRules(abc.ABC):
    name: str
    rows: int = 8
    cols: int = 8
    n_tokens: int
    token_weights: np.ndarray
    playout_max_plies: int
    background_max_plies: int

    @property
    def n_squares(self) -> int:
        return self.rows * self.cols

    @abc.abstractmethod
    def initial_state(self) -> GameState: ...

    @abc.abstractmethod
    def state_from(self, board_state, ply: Optional[int] = None) -> GameState: ...
