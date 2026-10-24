"""AtomicRunner: background self-play that feeds the per-game VectorDatabase.

Each trace is played out completely on a private copy and only then committed in a
single database call, so the database never sees a half-finished game.  The runner
works from the current game position (set by ShiRi) and yields to the search.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional

from apps.shiri.abc.board_embedding import BoardEmbedding
from apps.shiri.database.vector_database import VectorDatabase
from apps.shiri.search.playout import build_trace, simulate

log = logging.getLogger(__name__)


class _Interrupt:
    """Looks like an Event to ``simulate``: set when stopped or paused."""

    def __init__(self, stop: threading.Event, run: threading.Event):
        self._stop, self._run = stop, run

    def is_set(self) -> bool:
        return self._stop.is_set() or not self._run.is_set()


class AtomicRunner:
    def __init__(self, rules, embedding: BoardEmbedding, db: VectorDatabase,
                 seed: Optional[int] = None, stride: int = 3):
        self.rules = rules
        self.embedding = embedding
        self.db = db
        self.stride = stride
        self.rng = random.Random(seed)
        self.traces_run = 0
        self._root = None
        self._stop = threading.Event()
        self._run = threading.Event()
        self._run.set()  # runnable until ShiRi pauses it during a search
        self._interrupt = _Interrupt(self._stop, self._run)
        self._thread: Optional[threading.Thread] = None

    def set_root(self, state) -> None:
        self._root = state.copy()

    def run_one_trace(self) -> bool:
        """Play and commit one trace synchronously; False if it was interrupted or undecided."""
        root = self._root if self._root is not None else self.rules.initial_state()
        records: list = []
        sim = simulate(root, self.rng, self.rules.background_max_plies,
                       stop=self._interrupt, records=records, stride=self.stride)
        if sim.aborted or sim.outcome is None or not records:
            return False
        self.db.commit_trace(build_trace(records, sim.outcome, self.embedding))
        self.traces_run += 1
        return True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._run.set()
        self._thread = threading.Thread(target=self._loop, name="shiri-atomic-runner", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def stop(self) -> None:
        self._stop.set()
        self._run.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._run.wait(timeout=0.1):
                continue
            try:
                self.run_one_trace()
            except Exception:  # a bad trace must never kill the game
                log.exception("ShiRi AtomicRunner trace failed")
                time.sleep(0.2)
            time.sleep(0.001)  # let the event loop / search threads breathe
