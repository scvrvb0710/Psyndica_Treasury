"""Multi-timeframe Sats Ratchet: 15m execution, 4h swing, 1d trend filter.

  15m  base bars: tickets are managed here (finer fills), the scalp leg runs here
  4h   the swing leg runs on 4h bars resampled from the base
  1d   the trend filter: in an efficient daily uptrend every sell signal is
       blocked - the higher timeframe takes precedence (doctrine), and
       fading a daily trend is how stacks shrink

Same portfolio, same sleeve, same doctrine limits as the single-timeframe
engine. The 72h hold cap is enforced in seconds whatever the timeframe.
"""

from __future__ import annotations

from typing import Optional

from ..config import EngineConfig
from ..models import Bar, OnChainSnapshot
from ..signals.technical import EMA, EfficiencyRatio
from ..timeframes import TIMEFRAMES, Resampler
from .accumulator import SatsRatchet
from .swing import TicketPlan

DAILY_EMA = 50
DAILY_ER = 20


class DailyTrend:
    def __init__(self, er_block: float):
        self.er_block = er_block
        self.ema = EMA(DAILY_EMA)
        self.er = EfficiencyRatio(DAILY_ER)
        self.close: Optional[float] = None

    def update(self, bar: Bar) -> None:
        self.close = bar.close
        self.ema.update(bar.close)
        self.er.update(bar.close)

    @property
    def regime(self) -> str:
        if self.ema.value is None or self.er.value is None:
            return "warming_up"
        if self.er.value > self.er_block:
            return "trending_up" if self.close > self.ema.value else "trending_down"
        return "choppy"


class MultiTimeframeRatchet(SatsRatchet):
    def __init__(
        self,
        config: EngineConfig,
        initial_btc: float,
        swing_seconds: int = TIMEFRAMES["4h"],
        trend_seconds: int = TIMEFRAMES["1d"],
    ):
        super().__init__(config, initial_btc)
        base = config.bar_seconds
        self.swing_bars = Resampler(swing_seconds, base)
        self.trend_bars = Resampler(trend_seconds, base)
        self.daily = DailyTrend(config.swing.trend_er_block)
        self.leg_seconds = {"swing": swing_seconds, "scalp": base}
        self.last_swing_bar: Optional[Bar] = None
        self.blocked: list[TicketPlan] = []  # signals the daily filter stopped on the last bar

    def on_bar(self, bar: Bar, snaps: Optional[list[OnChainSnapshot]] = None) -> list[TicketPlan]:
        for snap in snaps or ():
            self.pressure.update(snap)
        pressure = self.pressure.last

        self._manage_tickets(bar)
        self._check_halt(bar)

        plans = []
        scalp = self.scalp.update(bar, pressure)
        if scalp:
            plans.append(scalp)
        for b4 in self.swing_bars.push(bar):
            self.last_swing_bar = b4
            swing = self.swing.update(b4, pressure)
            if swing:
                plans.append(swing)
        for bd in self.trend_bars.push(bar):
            self.daily.update(bd)

        self.blocked = []
        for plan in plans:
            if self.daily.regime == "trending_up":
                self.blocked.append(plan)
            else:
                self._open_ticket(bar, plan)

        self._dca(bar, pressure)
        self._bar_index += 1
        return plans


def is_multi_timeframe(config: EngineConfig) -> bool:
    return config.bar_seconds == TIMEFRAMES["15m"]


def engine_class(config: EngineConfig):
    """15m base bars run the multi-timeframe engine; anything else runs the
    single-timeframe one on the bars as given."""
    return MultiTimeframeRatchet if is_multi_timeframe(config) else SatsRatchet


def warmup_hours(config: EngineConfig) -> int:
    """Hours of history before every indicator the engine uses is live."""
    if is_multi_timeframe(config):
        return max(DAILY_EMA * 24, config.swing.trend_ema_period * 4) + 24
    return config.swing.trend_ema_period * config.bar_seconds // 3600 + 24
