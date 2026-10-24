"""Similarity measures between unit-norm weighted-matrix descriptors."""
from __future__ import annotations

import numpy as np


class Similarity:
    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        if na < 1e-12 or nb < 1e-12:
            return 0.0
        return float(np.dot(a.ravel(), b.ravel()) / (na * nb))

    @staticmethod
    def max_cosine(stack: np.ndarray, query: np.ndarray) -> float:
        """Highest dot product between ``query`` and any row of ``stack`` (rows are unit vectors)."""
        if stack.shape[0] == 0:
            return 0.0
        return float(np.max(stack @ query))
