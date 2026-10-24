"""Scalp leg: short-horizon mean reversion, gated by efficiency and fees.

Scalping BTC is mostly a fee-donation scheme. This leg only fires when:
  * the market is inefficient (low efficiency ratio - it's churning, not going),
  * price pokes outside a Bollinger band,
  * the distance back to the mean beats `min_edge_multiple` x round-trip fees
    and the doctrine edge floor.
If fees are too high for the volatility on offer, it simply never trades.
"""

from __future__ import annotations

from typing import Optional

from ..config import FeeModel, ScalpParams
from ..models import Bar
from ..signals.onchain import PressureReading
from ..signals.technical import EfficiencyRatio, RollingStats
from .swing import TicketPlan


class ScalpSignal:
    def __init__(self, p: ScalpParams, fees: FeeModel, heat_size_boost: float):
        self.p = p
        self.fees = fees
        self.heat_size_boost = heat_size_boost
        self.bb = RollingStats(p.bb_period)
        self.er = EfficiencyRatio(p.er_period)

    def update(self, bar: Bar, pressure: PressureReading) -> Optional[TicketPlan]:
        self.bb.update(bar.close)
        er = self.er.update(bar.close)
        if not self.p.enabled or not self.bb.ready or er is None:
            return None
        mean, std = self.bb.mean, self.bb.std
        if std <= 0 or er > self.p.er_max:
            return None
        if bar.close < mean + self.p.bb_k * std:
            return None

        expected_move = (bar.close - mean) / bar.close
        floor = max(self.p.min_edge_multiple * self.fees.round_trip_cost(), self.fees.edge_floor())
        if expected_move < floor:
            return None

        # Heat during a choppy range is noise more often than signal, so the
        # scalp leg only uses half the boost the swing leg does.
        size_mult = 1.0 + 0.5 * self.heat_size_boost * max(pressure.heat, 0.0)
        return TicketPlan(
            kind="scalp",
            size_mult=size_mult,
            target=mean,
            stop=bar.close + self.p.stop_std * std,
            max_hold_bars=self.p.max_hold_bars,
            reason=f"bb_z={(bar.close - mean) / std:.2f} edge={expected_move:.4f} er={er:.2f}",
        )
