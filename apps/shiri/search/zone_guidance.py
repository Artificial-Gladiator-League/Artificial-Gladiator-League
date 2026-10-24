"""ZoneGuidance: scores candidate moves by how close their resulting position lies to the
database's win zone versus its loss zone (relative to the other candidates)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from apps.shiri.abc.board_embedding import BoardEmbedding
from apps.shiri.database.vector_database import VectorDatabase


@dataclass
class CandidateScores:
    moves: list
    children: list           # child states, aligned with ``moves``
    raw: np.ndarray          # absolute zone score per candidate
    relative: np.ndarray     # raw minus the candidate mean, scaled into [-1, 1]
    priors: np.ndarray       # softmax of ``relative`` (sums to 1)


class ZoneGuidance:
    def __init__(self, db: VectorDatabase, embedding: BoardEmbedding, window: int = 6, temperature: float = 0.6):
        self.db = db
        self.embedding = embedding
        self.window = window
        self.temperature = temperature

    def score_vector(self, vec: np.ndarray, depth: int) -> float:
        """Zone score in about [-1, 1]: win-zone proximity minus loss-zone proximity."""
        win, loss, draw = self.db.zone_similarities(vec, depth, self.window)
        z = win - loss
        if draw > max(win, loss):
            z *= 0.5  # closest to a drawn zone: the decisive evidence is weaker
        return z

    def score_state(self, state, perspective: int) -> float:
        return self.score_vector(self.embedding.vector(state, perspective), state.ply())

    def candidate_scores(self, state, moves: list) -> CandidateScores:
        perspective = state.turn()  # the player choosing among ``moves``
        children, raw = [], np.zeros(len(moves), dtype=np.float32)
        for i, m in enumerate(moves):
            child = state.copy()
            child.push(m)
            children.append(child)
            raw[i] = self.score_state(child, perspective)
        rel = raw - raw.mean() if len(raw) else raw
        peak = float(np.max(np.abs(rel))) if len(rel) else 0.0
        rel = rel / peak if peak > 1e-9 else np.zeros_like(rel)
        logits = rel / max(self.temperature, 1e-6)
        logits -= logits.max() if len(logits) else 0.0
        p = np.exp(logits)
        priors = p / p.sum() if len(p) else p
        return CandidateScores(list(moves), children, raw, rel, priors)
