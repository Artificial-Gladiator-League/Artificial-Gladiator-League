"""DistanceMatrix: all-pairs shortest-path hop distance over an AdjacencyMatrix."""
from __future__ import annotations

import numpy as np

from apps.shiri.matrices.adjacency_matrix import AdjacencyMatrix


class DistanceMatrix:
    def __init__(self, data: np.ndarray):
        self.data = np.asarray(data, dtype=np.float32)

    @classmethod
    def from_adjacency(cls, adjacency: AdjacencyMatrix) -> "DistanceMatrix":
        n = adjacency.size
        dist = np.where(adjacency.data > 0, 1.0, np.inf).astype(np.float32)
        np.fill_diagonal(dist, 0.0)
        for k in range(n):  # Floyd-Warshall, vectorised over (i, j)
            np.minimum(dist, dist[:, k:k + 1] + dist[k:k + 1, :], out=dist)
        return cls(dist)

    def kernel(self, decay: float = 2.0) -> np.ndarray:
        """Distance decay weights exp(-d / decay); unreachable pairs weigh zero."""
        d = np.where(np.isfinite(self.data), self.data, np.inf)
        return np.exp(-d / float(decay)).astype(np.float32)
