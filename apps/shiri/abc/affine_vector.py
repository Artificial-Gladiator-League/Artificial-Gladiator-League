"""AffineVector: a real vector carried together with an affine map x -> scale * x + offset."""
from __future__ import annotations

import numpy as np


class AffineVector:
    __slots__ = ("values", "scale", "offset")

    def __init__(self, values, scale: float = 1.0, offset: float = 0.0):
        self.values = np.asarray(values, dtype=np.float32)
        self.scale = float(scale)
        self.offset = float(offset)

    def materialize(self) -> np.ndarray:
        return (self.values * self.scale + self.offset).astype(np.float32)

    def apply(self, x) -> np.ndarray:
        return np.asarray(x, dtype=np.float32) * self.scale + self.offset

    def dot(self, x) -> float:
        return float(np.dot(self.materialize(), np.asarray(x, dtype=np.float32)))

    def compose(self, scale: float, offset: float = 0.0) -> "AffineVector":
        """Return the vector whose map is ``x -> scale * (self(x)) + offset``."""
        return AffineVector(self.values, self.scale * scale, self.offset * scale + offset)

    def normalized(self) -> "AffineVector":
        m = self.materialize()
        norm = float(np.linalg.norm(m))
        return AffineVector(m / norm) if norm > 0 else AffineVector(m)

    def __len__(self) -> int:
        return int(self.values.shape[0])

    def __repr__(self) -> str:
        return f"AffineVector(len={len(self)}, scale={self.scale}, offset={self.offset})"
