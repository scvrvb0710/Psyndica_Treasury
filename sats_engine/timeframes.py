"""Timeframes and bar resampling.

Higher timeframes are built from the base bars, aligned to UTC (4h bars at
00/04/08/..., daily bars at 00:00 UTC). A higher bar is emitted only once
it has closed, so nothing downstream ever sees an unfinished candle.
"""

from __future__ import annotations

from typing import Optional

from .models import Bar

TIMEFRAMES = {"15m": 900, "1h": 3600, "4h": 4 * 3600, "1d": 24 * 3600}


def tf_seconds(name: str) -> int:
    try:
        return TIMEFRAMES[name]
    except KeyError:
        raise ValueError(f"unknown timeframe {name!r} (use {', '.join(TIMEFRAMES)})") from None


def tf_name(seconds: int) -> str:
    for name, s in TIMEFRAMES.items():
        if s == seconds:
            return name
    return f"{seconds}s"


class Resampler:
    """Aggregate base bars into `seconds`-long bars."""

    def __init__(self, seconds: int, base_seconds: int):
        if seconds % base_seconds:
            raise ValueError("higher timeframe must be a multiple of the base timeframe")
        self.seconds = seconds
        self.base_seconds = base_seconds
        self._bucket: Optional[int] = None
        self._o = self._h = self._l = self._c = self._v = 0.0

    def _flush(self) -> Bar:
        return Bar(self._bucket, self._o, self._h, self._l, self._c, self._v)

    def push(self, bar: Bar) -> list[Bar]:
        """Add one base bar; return any higher bars that just closed.

        Normally that's zero or one. After a data gap it can include the
        unfinished previous bucket, flushed as-is rather than dropped.
        """
        out = []
        bucket = bar.ts - bar.ts % self.seconds
        if self._bucket is not None and bucket != self._bucket:
            out.append(self._flush())  # gap: previous bucket never got its last bar
            self._bucket = None
        if self._bucket is None:
            self._bucket = bucket
            self._o, self._h, self._l, self._c, self._v = bar.open, bar.high, bar.low, bar.close, bar.volume
        else:
            self._h = max(self._h, bar.high)
            self._l = min(self._l, bar.low)
            self._c = bar.close
            self._v += bar.volume
        if bar.ts + self.base_seconds == bucket + self.seconds:
            out.append(self._flush())
            self._bucket = None
        return out
