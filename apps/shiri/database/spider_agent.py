"""SpiderAgent: one-ply zone-greedy agent that can override the search's final choice."""
from __future__ import annotations

from typing import Optional


class SpiderAgent:
    def __init__(self, zone_guidance, min_score: float = 0.03, min_margin: float = 0.02):
        self.zone = zone_guidance
        self.min_score = min_score
        self.min_margin = min_margin

    def suggest_from(self, scores) -> Optional[tuple]:
        """Pick from precomputed CandidateScores. Returns (move, raw_score, margin) or None."""
        raw = scores.raw
        if len(raw) < 2:
            return None
        order = raw.argsort()[::-1]
        best, second = float(raw[order[0]]), float(raw[order[1]])
        if best < self.min_score or best - second < self.min_margin:
            return None
        return scores.moves[int(order[0])], best, best - second

    def suggest(self, state) -> Optional[tuple]:
        if not self.zone.db.is_active():
            return None
        return self.suggest_from(self.zone.candidate_scores(state, state.legal_moves()))
