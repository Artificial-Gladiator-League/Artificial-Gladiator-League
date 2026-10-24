"""TokenMatrix: (squares x tokens) weighted occupancy of a position."""
from __future__ import annotations

import numpy as np

from apps.shiri.abc.affine_vector import AffineVector
from apps.shiri.abc.tokenized_vector import TokenizedVector


class TokenMatrix:
    def __init__(self, data: np.ndarray):
        self.data = np.asarray(data, dtype=np.float32)

    @classmethod
    def from_tokens(cls, tokens: TokenizedVector, weights: AffineVector) -> "TokenMatrix":
        return cls(tokens.one_hot() * weights.materialize()[None, :])
