"""MCVSPlayer: UCT search guided by algebraic zones of the per-game VectorDatabase.

Core loop (once the database is active):
  * every expanded node scores all its children with ZoneGuidance (candidate weighted
    matrices -> relative zone scores) and turns them into PUCT priors,
  * selection maximises  Q + c_puct * prior * sqrt(N) / (1 + n) + zone_bias * zone / (1 + n),
  * leaves are evaluated by ``simulate_from`` (blended with the zone value) and backed up,
  * a fraction of the playouts is committed to the database as new traces,
  * an optional SpiderAgent may override the final move when the zones are decisive.
While the database is still cold the search is plain UCT.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from apps.shiri.abc.board_embedding import BoardEmbedding
from apps.shiri.constants import WHITE
from apps.shiri.database.spider_agent import SpiderAgent
from apps.shiri.database.vector_database import VectorDatabase
from apps.shiri.search.common import (
    SearchConfig, SearchResult, backpropagate, fallback_move, forced_choice,
)
from apps.shiri.search.playout import PlayoutResult, build_trace, simulate
from apps.shiri.search.tree_node import TreeNode
from apps.shiri.search.uct_player import UCTPlayer
from apps.shiri.search.zone_guidance import CandidateScores, ZoneGuidance
from apps.shiri.util.rewards import Rewards


class MCVSPlayer:
    def __init__(self, rules, embedding: BoardEmbedding, db: VectorDatabase, rng,
                 config: Optional[SearchConfig] = None):
        self.rules = rules
        self.embedding = embedding
        self.db = db
        self.rng = rng
        self.config = config or SearchConfig()
        self.zone = ZoneGuidance(db, embedding, self.config.zone_window, self.config.prior_temperature)
        self.spider = SpiderAgent(self.zone, self.config.spider_min_score, self.config.spider_min_margin)
        self.uct = UCTPlayer(rules, rng, self.config)

    # ------------------------------------------------------------------ search
    def choose(self, state, deadline: float, abort=None) -> SearchResult:
        outcomes = state.outcomes_after_moves()
        forced = forced_choice(state, outcomes)
        if forced is not None:
            return forced
        if not self.db.is_active():
            return self.uct.choose(state, deadline, outcomes, abort)

        root = TreeNode(None, None, state, mover=1 - state.turn())
        root_scores = self._expand(root, outcomes)
        iterations = 0
        while time.perf_counter() < deadline and not (abort is not None and abort.is_set()):
            node = root
            while node.expanded and node.result is None and node.children:
                node = self._select(node)
            if node.n == 0 and node.result is None:
                node.result = node.state.result()
            if node.result is None and not node.expanded:
                self._expand(node)
            if node.result is not None:
                white_value = Rewards.white_value(node.result)
            else:
                sim = self.simulate_from(node.state, deadline, abort)
                if sim.aborted:
                    break
                white_value = self._blend(node, sim.white_value)
            backpropagate(node, white_value)
            iterations += 1

        best = root.most_visited()
        if best is None or best.n == 0:
            return SearchResult(fallback_move(state, outcomes, self.rng), iterations, "mcvs")
        chosen, spider_used = best, False
        if self.config.spider_override:
            chosen, spider_used = self._spider_override(root, best, root_scores)
        return SearchResult(chosen.move, iterations, "mcvs", root_visits=root.n,
                            value=chosen.q(), spider_used=spider_used)

    # --------------------------------------------------------------- internals
    def _expand(self, node: TreeNode, outcomes: Optional[dict] = None) -> CandidateScores:
        moves = node.state.legal_moves()
        scores = self.zone.candidate_scores(node.state, moves)
        mover = node.state.turn()
        for i, (m, child_state) in enumerate(zip(scores.moves, scores.children)):
            res = outcomes.get(m) if outcomes else None
            node.children.append(TreeNode(
                node, m, child_state, mover,
                prior=float(scores.priors[i]), zone=float(scores.relative[i]),
                zraw=float(scores.raw[i]), result=res,  # other results are computed on first visit
            ))
        node.expanded = True
        return scores

    def _select(self, node: TreeNode) -> TreeNode:
        cfg = self.config
        root_n = math.sqrt(max(node.n, 1))
        fpu = 1.0 - node.q() if node.n else 0.5  # unvisited child: assume the parent's value is mirrored
        best, best_score = None, -1e18
        for ch in node.children:
            q = ch.w / ch.n if ch.n else fpu
            score = (q + cfg.c_puct * ch.prior * root_n / (1 + ch.n)
                     + cfg.zone_bias * (ch.zone or 0.0) / (1 + ch.n))
            if score > best_score:
                best, best_score = ch, score
        return best

    def _blend(self, node: TreeNode, white_value: float) -> float:
        """Mix the node's absolute zone score into the playout value (from White's view)."""
        beta = self.config.value_blend
        if beta <= 0 or node.parent is None:
            return white_value
        mover_value = Rewards.from_score(max(-1.0, min(1.0, node.zraw)))
        if node.mover != WHITE:
            mover_value = 1.0 - mover_value
        return (1.0 - beta) * white_value + beta * mover_value

    def simulate_from(self, state, deadline: float, abort=None) -> PlayoutResult:
        """Playout from ``state``; sometimes the playout is also committed as a database trace."""
        record = self.rng.random() < self.config.trace_prob
        records: list = []
        sim = simulate(state, self.rng, self.rules.playout_max_plies, deadline=deadline, stop=abort,
                       records=records if record else None,
                       stride=self.config.trace_stride if record else 0)
        if record and not sim.aborted and sim.outcome is not None and records:
            self.db.commit_trace(build_trace(records, sim.outcome, self.embedding))
        return sim

    def _spider_override(self, root: TreeNode, best: TreeNode, scores: CandidateScores):
        sug = self.spider.suggest_from(scores)
        if sug is None or sug[0] == best.move:
            return best, False
        cand = next((c for c in root.children if c.move == sug[0]), None)
        if cand is None or cand.n == 0:
            return best, False
        cfg = self.config
        if cand.n >= cfg.spider_min_visit_share * best.n and cand.q() >= best.q() - cfg.spider_max_q_drop:
            return cand, True
        return best, False
