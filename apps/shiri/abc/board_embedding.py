"""BoardEmbedding: turns the site's chess / Breakthrough positions into unit vectors."""
from __future__ import annotations

import numpy as np

from apps.shiri.abc.affine_vector import AffineVector
from apps.shiri.abc.snapshot import GameStateSnapshot
from apps.shiri.abc.tokenized_vector import TokenizedVector


class BoardEmbedding:
    """Static geometry (8x8 grid adjacency/distance) plus per-position embedding.

    Positions are always expressed from a chosen player's perspective (own vs opponent
    tokens, board flipped so that the own side sits on the same edge), which makes
    records of White and Black games directly comparable inside one database.
    """

    def __init__(self, rules, connected: bool = True, decay: float = 2.0, gamma: float = 0.5):
        # Imported lazily: the matrices layer depends on the abc layer.
        from apps.shiri.matrices.adjacency_matrix import AdjacencyMatrix
        from apps.shiri.matrices.distance_matrix import DistanceMatrix
        from apps.shiri.matrices.weighted_matrix import WeightedMatrixBuilder

        self.rules = rules
        self.connected = bool(connected)
        self.adjacency = AdjacencyMatrix.grid(rules.rows, rules.cols, diagonal=True)
        self.distance = DistanceMatrix.from_adjacency(self.adjacency)
        self.weights = AffineVector(rules.token_weights)
        self.builder = WeightedMatrixBuilder(
            self.adjacency, self.distance, rules.n_tokens, self.weights,
            decay=decay, gamma=gamma, connected=self.connected,
        )
        self.dim = rules.n_squares * rules.n_tokens

    def snapshot(self, state, perspective: int | None = None, with_fen: bool = False) -> GameStateSnapshot:
        p = state.turn() if perspective is None else perspective
        tokens = TokenizedVector(state.tokens(p), self.rules.n_tokens)
        conn = state.connectivity(p) if self.connected else None
        return GameStateSnapshot(
            game=self.rules.name,
            perspective=p,
            side_to_move=state.turn(),
            ply=state.ply(),
            tokens=tokens,
            connectivity=conn,
            fen=state.fen() if with_fen else "",
        )

    def weighted(self, snapshot: GameStateSnapshot):
        return self.builder.build(snapshot)

    def vector(self, state, perspective: int | None = None) -> np.ndarray:
        """Unit-norm float32 descriptor of ``state`` as seen by ``perspective``."""
        return self.builder.build(self.snapshot(state, perspective)).unit_vector()
