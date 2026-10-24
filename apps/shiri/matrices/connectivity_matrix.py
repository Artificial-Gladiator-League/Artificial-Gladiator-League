"""ConnectivityMatrix: which piece reaches which square in a concrete position."""
from __future__ import annotations

import numpy as np

from apps.shiri.matrices.matrix_ops import MatrixOps


class ConnectivityMatrix:
    def __init__(self, data: np.ndarray):
        self.data = np.asarray(data, dtype=np.float32)

    @classmethod
    def empty(cls, n: int) -> "ConnectivityMatrix":
        return cls(np.zeros((n, n), dtype=np.float32))

    @classmethod
    def from_pairs(cls, n: int, pairs) -> "ConnectivityMatrix":
        c = np.zeros((n, n), dtype=np.float32)
        for i, j in pairs:
            c[i, j] = 1.0
        return cls(c)

    def operator(self, gamma: float = 0.5) -> np.ndarray:
        """I + gamma * rownorm(C): keeps the piece itself and adds the squares it reaches."""
        n = self.data.shape[0]
        return (np.eye(n, dtype=np.float32) + gamma * MatrixOps.row_normalize(self.data.copy())).astype(np.float32)
