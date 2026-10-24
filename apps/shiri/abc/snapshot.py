"""GameStateSnapshot: immutable, engine-independent view of a position used for embedding."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from apps.shiri.abc.tokenized_vector import TokenizedVector


@dataclass(frozen=True, eq=False)
class GameStateSnapshot:
    game: str
    perspective: int            # player whose point of view the tokens are expressed in
    side_to_move: int
    ply: int
    tokens: TokenizedVector     # own pieces / opponent pieces relative to ``perspective``
    connectivity: Optional[np.ndarray]  # (squares x squares) piece-to-square reach, or None
    fen: str
