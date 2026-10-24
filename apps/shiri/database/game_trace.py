"""NodeRecord and GameTrace: what the database stores and what a (self-play) game produces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from apps.shiri.constants import DRAW


@dataclass(eq=False)
class NodeRecord:
    """A position descriptor owned by ``mover`` (the player who just moved) at game depth ``depth``."""

    vector: np.ndarray   # unit-norm weighted-matrix descriptor
    depth: int
    mover: int
    count: int = 1


@dataclass(eq=False)
class GameTrace:
    records: list = field(default_factory=list)
    outcome: Optional[int] = None

    def add(self, vector: np.ndarray, depth: int, mover: int) -> None:
        self.records.append(NodeRecord(vector, int(depth), int(mover)))

    def finish(self, outcome: int) -> "GameTrace":
        self.outcome = outcome
        return self

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    @property
    def decisive(self) -> bool:
        return self.outcome is not None and self.outcome != DRAW

    def __len__(self) -> int:
        return len(self.records)
