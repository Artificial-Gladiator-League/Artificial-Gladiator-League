"""Shared search types and helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from apps.shiri.util.rewards import Rewards


@dataclass
class SearchConfig:
    c_uct: float = 1.4          # UCB1 exploration constant (rewards live in [0, 1])
    c_puct: float = 1.5         # PUCT exploration constant
    zone_bias: float = 0.25     # weight of the relative zone score inside selection
    value_blend: float = 0.15   # share of the zone value mixed into a leaf evaluation
    prior_temperature: float = 0.6
    zone_window: int = 6        # plies around a node's depth searched in the database
    trace_prob: float = 0.25    # chance that a search playout is recorded as a database trace
    trace_stride: int = 3       # record every n-th ply of a recorded playout
    spider_override: bool = True
    spider_min_score: float = 0.03
    spider_min_margin: float = 0.02
    spider_min_visit_share: float = 0.25
    spider_max_q_drop: float = 0.10


@dataclass
class SearchResult:
    move: Any
    iterations: int
    mode: str                   # "uct", "mcvs", "forced" or "mate"
    root_visits: int = 0
    value: float = 0.5
    spider_used: bool = False


def forced_choice(state, outcomes: dict) -> Optional[SearchResult]:
    """Single legal move, or a move that wins on the spot: no search needed."""
    moves = state.legal_moves()
    if not moves:
        raise ValueError("No legal moves available")
    if len(moves) == 1:
        return SearchResult(moves[0], 0, "forced")
    me = state.turn()
    for m in moves:
        if outcomes.get(m) == me:
            return SearchResult(m, 0, "mate", value=1.0)
    return None


def backpropagate(node, white_value: float) -> None:
    while node is not None:
        node.n += 1
        node.w += Rewards.for_player(white_value, node.mover)
        node = node.parent


def fallback_move(state, outcomes: dict, rng):
    """Used only if the budget expired before one iteration completed."""
    me = state.turn()
    moves = state.legal_moves()
    safe = [m for m in moves if outcomes.get(m) is None or outcomes[m] == me]
    return rng.choice(safe or moves)
