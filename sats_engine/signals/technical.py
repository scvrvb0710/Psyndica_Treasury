"""Streaming technical indicators (stdlib only, O(1) per update where possible)."""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Optional


class EMA:
    def __init__(self, period: int):
        self.alpha = 2.0 / (period + 1)
        self.period = period
        self.value: Optional[float] = None
        self._seed: list[float] = []

    def update(self, x: float) -> Optional[float]:
        if self.value is None:
            # Seed with an SMA so early values aren't dominated by the first print.
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


class ATR:
    """Wilder's average true range."""

    def __init__(self, period: int):
        self.period = period
        self.value: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._seed: list[float] = []

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class RSI:
    """Wilder's RSI."""

    def __init__(self, period: int):
        self.period = period
        self.value: Optional[float] = None
        self._prev: Optional[float] = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class EfficiencyRatio:
    """Kaufman efficiency ratio: net move / sum of absolute moves over `period`.

    ~1.0 means price travelled in a straight line (efficient, trending);
    ~0.0 means it went nowhere while churning (inefficient, mean-reverting).
    This is the engine's measure of market efficiency: swing fades and
    scalps are only taken when the market is inefficient enough to revert.
    """

    def __init__(self, period: int):
        self.period = period
        self._closes: Deque[float] = deque(maxlen=period + 1)
        self._abs_moves: Deque[float] = deque(maxlen=period)
        self._noise = 0.0
        self.value: Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        if self._closes:
            move = abs(close - self._closes[-1])
            if len(self._abs_moves) == self.period:
                self._noise -= self._abs_moves[0]
            self._abs_moves.append(move)
            self._noise += move
        self._closes.append(close)
        if len(self._closes) <= self.period:
            return None
        net = abs(self._closes[-1] - self._closes[0])
        self.value = net / self._noise if self._noise > 1e-12 else 0.0
        return self.value


class RollingStats:
    """Rolling mean / stdev / z-score over a fixed window."""

    def __init__(self, window: int):
        self.window = window
        self._xs: Deque[float] = deque(maxlen=window)
        self._sum = 0.0
        self._sumsq = 0.0

    def update(self, x: float) -> None:
        if len(self._xs) == self.window:
            old = self._xs[0]
            self._sum -= old
            self._sumsq -= old * old
        self._xs.append(x)
        self._sum += x
        self._sumsq += x * x

    @property
    def count(self) -> int:
        return len(self._xs)

    @property
    def ready(self) -> bool:
        return len(self._xs) == self.window

    @property
    def mean(self) -> float:
        return self._sum / len(self._xs) if self._xs else 0.0

    @property
    def std(self) -> float:
        n = len(self._xs)
        if n < 2:
            return 0.0
        var = (self._sumsq - self._sum * self._sum / n) / (n - 1)
        return math.sqrt(max(var, 0.0))

    def z(self, x: float) -> float:
        s = self.std
        return (x - self.mean) / s if s > 1e-12 else 0.0

    @property
    def max(self) -> float:
        return max(self._xs) if self._xs else 0.0
