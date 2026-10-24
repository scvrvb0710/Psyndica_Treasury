"""Swing leg: fade over-extended rallies in inefficient markets, rebuy lower.

Sells only when all of these line up:
  * close is `entry_z` ATRs above the swing EMA (stretched),
  * RSI is overbought,
  * the market is NOT an efficient uptrend (selling into a clean trend is
    how sats get lost - price never comes back).
On-chain heat scales the size: distribution-like activity at a stretched
high is the setup the engine wants to lean into.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import FeeModel, SwingParams
from ..models import Bar
from ..signals.onchain import PressureReading
from ..signals.technical import ATR, EMA, RSI, EfficiencyRatio


@dataclass(frozen=True)
class TicketPlan:
    kind: str
    size_mult: float  # multiplier on the leg's base size
    target: float
    stop: float
    max_hold_bars: int
    reason: str


class SwingSignal:
    def __init__(self, p: SwingParams, fees: FeeModel, heat_size_boost: float):
        self.p = p
        self.fees = fees
        self.heat_size_boost = heat_size_boost
        self.ema = EMA(p.ema_period)
        self.trend = EMA(p.trend_ema_period)
        self.atr = ATR(p.atr_period)
        self.rsi = RSI(p.rsi_period)
        self.er = EfficiencyRatio(p.er_period)
        self.last_close: Optional[float] = None

    def update(self, bar: Bar, pressure: PressureReading) -> Optional[TicketPlan]:
        self.last_close = bar.close
        ema = self.ema.update(bar.close)
        trend = self.trend.update(bar.close)
        atr = self.atr.update(bar.high, bar.low, bar.close)
        rsi = self.rsi.update(bar.close)
        er = self.er.update(bar.close)
        if not self.p.enabled or None in (ema, trend, atr, rsi, er) or atr <= 0:
            return None

        stretch = (bar.close - ema) / atr
        efficient_uptrend = bar.close > trend and er > self.p.trend_er_block
        if stretch < self.p.entry_z or rsi < self.p.entry_rsi or efficient_uptrend:
            return None

        target = bar.close - self.p.target_atr * atr
        # The buyback must clear fees with room to spare or the trade is pointless.
        max_target = bar.close * (1 - self.fees.edge_floor())
        target = min(target, max_target)
        if target <= 0:
            return None
        stop = bar.close + self.p.stop_atr * atr

        size_mult = 1.0 + self.heat_size_boost * max(pressure.heat, 0.0)
        return TicketPlan(
            kind="swing",
            size_mult=size_mult,
            target=target,
            stop=stop,
            max_hold_bars=self.p.max_hold_bars,
            reason=f"stretch={stretch:.2f}atr rsi={rsi:.0f} er={er:.2f} heat={pressure.heat:+.2f}",
        )
