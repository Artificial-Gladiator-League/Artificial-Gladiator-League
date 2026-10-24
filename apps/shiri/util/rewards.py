"""Reward conventions shared by the search and the database layers."""
from __future__ import annotations

import math

from apps.shiri.constants import BLACK, DRAW, WHITE, ZONE_DRAW, ZONE_LOSS, ZONE_WIN


class Rewards:
    WIN = 1.0
    DRAW = 0.5
    LOSS = 0.0

    @staticmethod
    def white_value(outcome: int) -> float:
        """Value of a finished game from White's point of view."""
        if outcome == WHITE:
            return Rewards.WIN
        if outcome == BLACK:
            return Rewards.LOSS
        return Rewards.DRAW

    @staticmethod
    def for_player(white_value: float, player: int) -> float:
        return white_value if player == WHITE else 1.0 - white_value

    @staticmethod
    def from_score(score: float) -> float:
        """Map a heuristic score in [-1, 1] to a reward in [0, 1]."""
        return min(1.0, max(0.0, 0.5 + 0.5 * score))

    @staticmethod
    def squash(diff: float, scale: float) -> float:
        return math.tanh(diff / scale)

    @staticmethod
    def zone_of(outcome: int, mover: int) -> str:
        """Database partition of a record owned by ``mover`` in a game won by ``outcome``."""
        if outcome == DRAW:
            return ZONE_DRAW
        return ZONE_WIN if outcome == mover else ZONE_LOSS
