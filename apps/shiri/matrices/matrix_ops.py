"""MatrixOps: small shared numpy helpers for the matrices layer."""
from __future__ import annotations

import numpy as np


class MatrixOps:
    @staticmethod
    def frobenius(m: np.ndarray) -> float:
        return float(np.linalg.norm(m))

    @staticmethod
    def unit(v: np.ndarray) -> np.ndarray:
        """Unit-norm float32 copy of ``v`` (all-zero input stays all-zero)."""
        v = np.asarray(v, dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 1e-12 else np.zeros_like(v)

    @staticmethod
    def flatten_unit(m: np.ndarray) -> np.ndarray:
        return MatrixOps.unit(np.asarray(m, dtype=np.float32).reshape(-1))

    @staticmethod
    def row_normalize(m: np.ndarray) -> np.ndarray:
        sums = m.sum(axis=1, keepdims=True)
        sums[sums == 0] = 1.0
        return (m / sums).astype(np.float32)

    @staticmethod
    def hadamard(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return (a * b).astype(np.float32)

    @staticmethod
    def chain(*mats: np.ndarray) -> np.ndarray:
        out = mats[0]
        for m in mats[1:]:
            out = out @ m
        return out.astype(np.float32)

    @staticmethod
    def quantized_key(v: np.ndarray) -> bytes:
        """Stable hash key for near-identical unit vectors (used for de-duplication)."""
        return np.round(np.asarray(v) * 127.0).astype(np.int8).tobytes()
