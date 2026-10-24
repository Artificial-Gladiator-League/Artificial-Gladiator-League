"""Playouts (simulateFrom) shared by UCT, MCVS and the AtomicRunner."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from apps.shiri.abc.board_embedding import BoardEmbedding
from apps.shiri.constants import BLACK, WHITE
from apps.shiri.database.game_trace import GameTrace
from apps.shiri.util.rewards import Rewards

ADJUDICATION_MARGIN = 0.6  # |material score| needed to label a truncated playout win/loss


@dataclass
class PlayoutResult:
    white_value: float
    outcome: Optional[int]   # WHITE / BLACK / DRAW when decided (or adjudicated), else None
    terminal: bool
    aborted: bool = False


def simulate(state, rng, max_plies: int, deadline: Optional[float] = None, stop=None,
             records: Optional[list] = None, stride: int = 0) -> PlayoutResult:
    """Play ``state`` out with the rules' playout policy on a private copy.

    ``records`` (with ``stride``) collects ``(state_copy, mover)`` pairs for trace building.
    Stops with ``aborted=True`` when ``deadline`` passes or ``stop.is_set()``.
    """
    s = state.copy()
    plies = 0
    while True:
        r = s.result()
        if r is not None:
            return PlayoutResult(Rewards.white_value(r), r, True)
        if plies >= max_plies:
            break
        if (plies & 7) == 0:
            if (deadline is not None and time.perf_counter() >= deadline) or (stop is not None and stop.is_set()):
                return PlayoutResult(0.5, None, False, aborted=True)
        mover = s.turn()
        s.push(s.playout_move(rng))
        plies += 1
        if stride and records is not None and plies % stride == 0:
            records.append((s.copy(), mover))
    score = s.material_score(WHITE)
    outcome = WHITE if score >= ADJUDICATION_MARGIN else BLACK if score <= -ADJUDICATION_MARGIN else None
    return PlayoutResult(Rewards.from_score(score), outcome, False)


def build_trace(records: list, outcome: int, embedding: BoardEmbedding) -> GameTrace:
    trace = GameTrace()
    for st, mover in records:
        trace.add(embedding.vector(st, mover), st.ply(), mover)
    return trace.finish(outcome)
