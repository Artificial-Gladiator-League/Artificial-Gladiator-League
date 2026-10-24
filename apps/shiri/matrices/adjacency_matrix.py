"""AdjacencyMatrix: static board-geometry adjacency (squares x squares)."""
from __future__ import annotations

import numpy as np

from apps.shiri.matrices.matrix_ops import MatrixOps


class AdjacencyMatrix:
    def __init__(self, data: np.ndarray):
        self.data = np.asarray(data, dtype=np.float32)

    @classmethod
    def grid(cls, rows: int, cols: int, diagonal: bool = True, self_loops: bool = True) -> "AdjacencyMatrix":
        n = rows * cols
        a = np.zeros((n, n), dtype=np.float32)
        for r in range(rows):
            for c in range(cols):
                i = r * cols + c
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        if not diagonal and dr != 0 and dc != 0:
                            continue
                        rr, cc = r + dr, c + dc
                        if 0 <= rr < rows and 0 <= cc < cols:
                            a[i, rr * cols + cc] = 1.0
        if self_loops:
            np.fill_diagonal(a, 1.0)
        return cls(a)

    @property
    def size(self) -> int:
        return int(self.data.shape[0])

    def row_normalized(self) -> np.ndarray:
        return MatrixOps.row_normalize(self.data.copy())
