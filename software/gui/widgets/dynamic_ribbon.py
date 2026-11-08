"""Rolling mean trajectory curve with +/-1 sigma confidence envelope."""

from __future__ import annotations

from collections import deque
from typing import Deque

import numpy as np


class DynamicRibbon:
    """Computes rolling mean and +/-1 sigma bounds over a sliding window."""

    def __init__(self, window: int = 10, max_points: int = 600) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        self.window = window
        self.x: Deque[float] = deque(maxlen=max_points)
        self.raw: Deque[float] = deque(maxlen=max_points)
        self.mean: Deque[float] = deque(maxlen=max_points)
        self.upper: Deque[float] = deque(maxlen=max_points)
        self.lower: Deque[float] = deque(maxlen=max_points)

    def clear(self) -> None:
        self.x.clear()
        self.raw.clear()
        self.mean.clear()
        self.upper.clear()
        self.lower.clear()

    def append(self, t: float, value: float) -> tuple[float, float, float]:
        self.x.append(float(t))
        self.raw.append(float(value))
        tail = list(self.raw)[-self.window :]
        arr = np.asarray(tail, dtype=float)
        finite = arr[np.isfinite(arr)]
        if len(finite) == 0:
            mu, sigma = float("nan"), 0.0
        else:
            mu = float(np.mean(finite))
            sigma = float(np.std(finite))
        hi = mu + sigma
        lo = mu - sigma
        self.mean.append(mu)
        self.upper.append(hi)
        self.lower.append(lo)
        return mu, lo, hi

