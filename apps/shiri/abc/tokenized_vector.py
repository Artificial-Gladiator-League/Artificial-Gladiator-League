"""TokenizedVector: a board flattened to one integer token per square (0 = empty)."""
from __future__ import annotations

import numpy as np


class TokenizedVector:
    __slots__ = ("_tokens", "n_tokens")

    def __init__(self, tokens, n_tokens: int):
        arr = np.array(tokens, dtype=np.int8)
        arr.setflags(write=False)
        self._tokens = arr
        self.n_tokens = int(n_tokens)

    @property
    def tokens(self) -> np.ndarray:
        return self._tokens

    def __len__(self) -> int:
        return int(self._tokens.shape[0])

    def occupied(self) -> np.ndarray:
        return self._tokens > 0

    def one_hot(self) -> np.ndarray:
        """(squares x n_tokens) indicator matrix; empty squares are all-zero rows."""
        out = np.zeros((len(self), self.n_tokens), dtype=np.float32)
        idx = np.nonzero(self._tokens)[0]
        out[idx, self._tokens[idx] - 1] = 1.0
        return out

    def counts(self) -> np.ndarray:
        return np.bincount(self._tokens, minlength=self.n_tokens + 1)[1:].astype(np.int32)

    def hamming(self, other: "TokenizedVector") -> int:
        return int(np.count_nonzero(self._tokens != other._tokens))

    def key(self) -> bytes:
        return self._tokens.tobytes()

    def __eq__(self, other) -> bool:
        return isinstance(other, TokenizedVector) and self.n_tokens == other.n_tokens and self.key() == other.key()

    def __hash__(self) -> int:
        return hash((self.n_tokens, self.key()))

    def __repr__(self) -> str:
        return f"TokenizedVector(n={len(self)}, tokens={self.n_tokens})"
