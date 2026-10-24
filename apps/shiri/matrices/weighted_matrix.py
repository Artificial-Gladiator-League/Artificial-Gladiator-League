"""WeightedMatrix and WeightedMatrixBuilder.

classic    W = A x D x T          (adjacency x distance kernel x token matrix)
connected  W = A x D x C' x T     (C' = I + gamma * rownorm(C), the position's connectivity)

C is squares x squares, so it sits between D and T to keep the product conformable.
"""
from __future__ import annotations

import numpy as np

from apps.shiri.abc.affine_vector import AffineVector
from apps.shiri.abc.snapshot import GameStateSnapshot
from apps.shiri.matrices.adjacency_matrix import AdjacencyMatrix
from apps.shiri.matrices.connectivity_matrix import ConnectivityMatrix
from apps.shiri.matrices.distance_matrix import DistanceMatrix
from apps.shiri.matrices.matrix_ops import MatrixOps
from apps.shiri.matrices.token_matrix import TokenMatrix


class WeightedMatrix:
    __slots__ = ("data", "connected")

    def __init__(self, data: np.ndarray, connected: bool = False):
        self.data = np.asarray(data, dtype=np.float32)
        self.connected = connected

    @property
    def shape(self):
        return self.data.shape

    def unit_vector(self) -> np.ndarray:
        return MatrixOps.flatten_unit(self.data)


class WeightedMatrixBuilder:
    def __init__(
        self,
        adjacency: AdjacencyMatrix,
        distance: DistanceMatrix,
        n_tokens: int,
        weights: AffineVector,
        decay: float = 2.0,
        gamma: float = 0.5,
        connected: bool = False,
    ):
        self.n_tokens = n_tokens
        self.weights = weights
        self.gamma = gamma
        self.connected = connected
        # A x D never changes during a game, so it is multiplied out once.
        self._ad = MatrixOps.chain(adjacency.row_normalized(), MatrixOps.row_normalize(distance.kernel(decay)))

    def classic(self, snapshot: GameStateSnapshot) -> WeightedMatrix:
        t = TokenMatrix.from_tokens(snapshot.tokens, self.weights)
        return WeightedMatrix(self._ad @ t.data, connected=False)

    def connected_matrix(self, snapshot: GameStateSnapshot) -> WeightedMatrix:
        t = TokenMatrix.from_tokens(snapshot.tokens, self.weights)
        c = ConnectivityMatrix(snapshot.connectivity).operator(self.gamma)
        return WeightedMatrix(self._ad @ c @ t.data, connected=True)

    def build(self, snapshot: GameStateSnapshot) -> WeightedMatrix:
        if self.connected and snapshot.connectivity is not None:
            return self.connected_matrix(snapshot)
        return self.classic(snapshot)
