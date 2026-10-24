"""VectorDatabase: per-game win / loss / draw partitions of weighted-matrix descriptors.

One instance lives exactly as long as one game: ``ShiRi.start_new_game`` builds a fresh,
empty database, nothing is ever loaded from or saved to disk.  All public methods are
thread-safe because the AtomicRunner commits traces while the search reads.
"""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from apps.shiri.constants import ZONE_DRAW, ZONE_LOSS, ZONE_WIN, ZONES
from apps.shiri.database.game_trace import GameTrace
from apps.shiri.matrices.matrix_ops import MatrixOps
from apps.shiri.util.rewards import Rewards


class _Partition:
    def __init__(self, dim: int, capacity: int = 256):
        self.dim = dim
        self.n = 0
        self.mat = np.zeros((capacity, dim), dtype=np.float32)
        self.depth = np.zeros(capacity, dtype=np.int16)
        self.count = np.zeros(capacity, dtype=np.int32)
        self.stamp = np.zeros(capacity, dtype=np.int64)
        self.index: dict[tuple, int] = {}

    def _grow(self) -> None:
        cap = self.mat.shape[0] * 2
        self.mat = np.resize(self.mat, (cap, self.dim))
        self.depth = np.resize(self.depth, cap)
        self.count = np.resize(self.count, cap)
        self.stamp = np.resize(self.stamp, cap)

    def add(self, vec: np.ndarray, depth: int, count: int, stamp: int) -> bool:
        """Insert a record; identical (vector, depth) records only bump a counter. True if new."""
        key = (depth, MatrixOps.quantized_key(vec))
        row = self.index.get(key)
        if row is not None:
            self.count[row] += count
            self.stamp[row] = stamp
            return False
        if self.n == self.mat.shape[0]:
            self._grow()
        self.mat[self.n] = vec
        self.depth[self.n] = depth
        self.count[self.n] = count
        self.stamp[self.n] = stamp
        self.index[key] = self.n
        self.n += 1
        return True

    def max_similarity(self, q: np.ndarray, depth: int, window: int) -> Optional[float]:
        if self.n == 0:
            return None
        d = self.depth[:self.n]
        mask = np.abs(d.astype(np.int32) - depth) <= window
        if not mask.any():
            return None
        return float(np.max(self.mat[:self.n][mask] @ q))

    def prune_to(self, limit: int) -> int:
        """Drop the least reinforced (lowest count), then oldest, records beyond ``limit``."""
        if self.n <= limit:
            return 0
        order = np.lexsort((self.stamp[:self.n], self.count[:self.n]))  # ascending: weakest first
        keep = np.sort(order[self.n - limit:])
        removed = self.n - limit
        self.mat[:limit] = self.mat[keep]
        self.depth[:limit] = self.depth[keep]
        self.count[:limit] = self.count[keep]
        self.stamp[:limit] = self.stamp[keep]
        self.n = limit
        self.index = {
            (int(self.depth[i]), MatrixOps.quantized_key(self.mat[i])): i for i in range(limit)
        }
        return removed


class VectorDatabase:
    def __init__(self, dim: int, max_per_zone: int = 20000, min_active_records: int = 120):
        self.dim = dim
        self.max_per_zone = max_per_zone
        self.min_active_records = min_active_records
        self._lock = threading.RLock()
        self._zones = {z: _Partition(dim) for z in ZONES}
        self._stamp = 0
        self.traces_committed = 0

    def commit_trace(self, trace: GameTrace) -> int:
        """Atomically add every record of a finished trace; returns the number of new rows."""
        if not trace.finished or not len(trace):
            return 0
        added = 0
        with self._lock:
            self._stamp += 1
            for rec in trace.records:
                zone = Rewards.zone_of(trace.outcome, rec.mover)
                if self._zones[zone].add(rec.vector, rec.depth, rec.count, self._stamp):
                    added += 1
            self.traces_committed += 1
            for part in self._zones.values():
                if part.n > self.max_per_zone * 1.1:
                    part.prune_to(self.max_per_zone)
        return added

    def max_similarity_near_depth(self, zone: str, vec: np.ndarray, depth: int, window: int = 6) -> float:
        """Highest similarity between ``vec`` and any ``zone`` record within ``window`` plies of ``depth``.

        Returns 0.0 when the zone holds no evidence for that depth range.
        """
        with self._lock:
            sim = self._zones[zone].max_similarity(vec, depth, window)
        return 0.0 if sim is None else sim

    def zone_similarities(self, vec: np.ndarray, depth: int, window: int = 6) -> tuple[float, float, float]:
        with self._lock:
            sims = [self._zones[z].max_similarity(vec, depth, window) for z in (ZONE_WIN, ZONE_LOSS, ZONE_DRAW)]
        return tuple(0.0 if s is None else s for s in sims)

    def prune(self, max_per_zone: Optional[int] = None) -> int:
        limit = self.max_per_zone if max_per_zone is None else max_per_zone
        with self._lock:
            return sum(p.prune_to(limit) for p in self._zones.values())

    def size(self, zone: Optional[str] = None) -> int:
        with self._lock:
            if zone is not None:
                return self._zones[zone].n
            return sum(p.n for p in self._zones.values())

    def is_active(self) -> bool:
        """Enough evidence (and at least one decisive zone) for zone guidance to be meaningful."""
        with self._lock:
            decisive = self._zones[ZONE_WIN].n + self._zones[ZONE_LOSS].n
            return decisive > 0 and self.size() >= self.min_active_records

    def clear(self) -> None:
        with self._lock:
            self._zones = {z: _Partition(self.dim) for z in ZONES}
            self.traces_committed = 0
