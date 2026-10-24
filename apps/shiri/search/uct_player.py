"""UCTPlayer: plain UCT with random playouts. Also the cold-start fallback of MCVSPlayer."""
from __future__ import annotations

import math
import time
from typing import Optional

from apps.shiri.search.common import (
    SearchConfig, SearchResult, backpropagate, fallback_move, forced_choice,
)
from apps.shiri.search.playout import simulate
from apps.shiri.search.tree_node import TreeNode
from apps.shiri.util.rewards import Rewards


class UCTPlayer:
    def __init__(self, rules, rng, config: Optional[SearchConfig] = None):
        self.rules = rules
        self.rng = rng
        self.config = config or SearchConfig()

    def choose(self, state, deadline: float, outcomes: Optional[dict] = None, abort=None) -> SearchResult:
        outcomes = state.outcomes_after_moves() if outcomes is None else outcomes
        forced = forced_choice(state, outcomes)
        if forced is not None:
            return forced
        root = TreeNode(None, None, state, mover=1 - state.turn())
        iterations = 0
        while time.perf_counter() < deadline and not (abort is not None and abort.is_set()):
            node = root
            while node.result is None and node.untried is not None and not node.untried and node.children:
                node = self._select(node)
            if node.result is None:
                if node.untried is None:
                    node.untried = list(node.state.legal_moves())
                    self.rng.shuffle(node.untried)
                if node.untried:
                    move = node.untried.pop()
                    child_state = node.state.copy()
                    child_state.push(move)
                    res = outcomes.get(move) if node is root else None
                    child = TreeNode(node, move, child_state, node.state.turn(),
                                     result=res if res is not None else child_state.result())
                    node.children.append(child)
                    node = child
            if node.result is not None:
                white_value = Rewards.white_value(node.result)
            else:
                sim = simulate(node.state, self.rng, self.rules.playout_max_plies, deadline=deadline, stop=abort)
                if sim.aborted:
                    break
                white_value = sim.white_value
            backpropagate(node, white_value)
            iterations += 1
        best = root.most_visited()
        if best is None or best.n == 0:
            return SearchResult(fallback_move(state, outcomes, self.rng), iterations, "uct")
        return SearchResult(best.move, iterations, "uct", root_visits=root.n, value=best.q())

    def _select(self, node: TreeNode) -> TreeNode:
        log_n = math.log(max(node.n, 1))
        c = self.config.c_uct
        return max(node.children, key=lambda ch: ch.w / ch.n + c * math.sqrt(log_n / ch.n))
