"""Backtester: run the Sats Ratchet over bars and score it in BTC.

The only question that matters: did the engine end with more BTC than
simply holding (and, if contributions are on, than plain DCA)?
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .config import EngineConfig
from .models import Bar, OnChainSnapshot, Ticket
from .strategy.accumulator import SatsRatchet


@dataclass
class LegStats:
    trips: int
    wins: int
    sats_delta_btc: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.trips if self.trips else 0.0


@dataclass
class BacktestResult:
    initial_btc: float
    final_stack_btc: float
    hodl_btc: float  # initial + plain DCA (same USD, same schedule)
    fees_usd: float
    legs: dict[str, LegStats]
    exits: dict[str, int]
    halted: bool
    max_drawdown: float  # of stack vs. benchmark, peak to trough
    engine: SatsRatchet
    base_rel: float = 1.0  # stack/benchmark when scoring began (see run(score_from=))

    @property
    def edge_vs_hodl(self) -> float:
        """Fractional BTC advantage over the HODL/DCA benchmark over the scored window."""
        if not self.hodl_btc:
            return 0.0
        return (self.final_stack_btc / self.hodl_btc) / self.base_rel - 1

    @property
    def trips(self) -> int:
        return sum(s.trips for s in self.legs.values())

    def summary(self) -> str:
        lines = [
            f"initial stack      {self.initial_btc:.8f} BTC",
            f"benchmark (HODL)   {self.hodl_btc:.8f} BTC",
            f"engine stack       {self.final_stack_btc:.8f} BTC",
            f"edge vs benchmark  {self.edge_vs_hodl * 100:+.3f}%",
            f"max BTC drawdown   {self.max_drawdown * 100:.2f}%",
            f"fees paid          ${self.fees_usd:,.2f}",
            f"halted             {'YES - human review required' if self.halted else 'no'}",
        ]
        for kind, s in self.legs.items():
            lines.append(
                f"{kind:<6} trips={s.trips:<4} win={s.win_rate * 100:5.1f}%  "
                f"net={s.sats_delta_btc * 1e8:+,.0f} sats"
            )
        if self.exits:
            lines.append("exits  " + "  ".join(f"{k}={v}" for k, v in sorted(self.exits.items())))
        return "\n".join(lines)


def _leg_stats(tickets: Sequence[Ticket]) -> dict[str, LegStats]:
    out: dict[str, LegStats] = {}
    for t in tickets:
        s = out.setdefault(t.kind, LegStats(0, 0, 0.0))
        s.trips += 1
        s.wins += t.sats_delta > 0
        s.sats_delta_btc += t.sats_delta
    return out


def run(
    bars: Sequence[Bar],
    snaps: Optional[Sequence[OnChainSnapshot]] = None,
    config: Optional[EngineConfig] = None,
    initial_btc: float = 1.0,
    score_from: int = 0,
) -> BacktestResult:
    """Run the engine over `bars`. Bars before `score_from` only warm up the
    indicators: edge, drawdown, and round-trip stats cover the rest."""
    if not bars:
        raise ValueError("no bars to backtest")
    if not 0 <= score_from < len(bars):
        raise ValueError("score_from must index into bars")
    cfg = config or EngineConfig()
    engine = SatsRatchet(cfg, initial_btc)
    snaps = sorted(snaps or (), key=lambda s: s.ts)

    hodl = initial_btc
    j = 0
    base_rel = 1.0
    peak_rel, max_dd = 1.0, 0.0
    for i, bar in enumerate(bars):
        if i == score_from:
            base_rel = engine.stack_btc(bars[i - 1].close) / hodl if i else 1.0
            peak_rel = base_rel
        # Feed only snapshots observed by this bar's close - no lookahead.
        bar_close_ts = bar.ts + cfg.bar_seconds
        batch = []
        while j < len(snaps) and snaps[j].ts <= bar_close_ts:
            batch.append(snaps[j])
            j += 1
        engine.on_bar(bar, batch)

        if cfg.dca.contribution_usd > 0 and i % cfg.dca.interval_bars == 0:
            price = bar.close * (1 + cfg.fees.slippage)
            hodl += cfg.dca.contribution_usd * (1 - cfg.fees.taker) / price

        stack = engine.stack_btc(bar.close)
        # Drawdown is measured relative to the benchmark so contributions and
        # price moves don't masquerade as strategy gains or losses.
        rel = stack / hodl
        if i >= score_from:
            peak_rel = max(peak_rel, rel)
            max_dd = max(max_dd, 1 - rel / peak_rel)

    last = bars[-1].close
    scored = [t for t in engine.closed if t.opened_ts >= bars[score_from].ts]
    exits: dict[str, int] = {}
    for t in scored:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    return BacktestResult(
        initial_btc=initial_btc,
        final_stack_btc=engine.stack_btc(last),
        hodl_btc=hodl,
        fees_usd=sum(f.fee_usd for f in engine.fills),
        legs=_leg_stats(scored),
        exits=exits,
        halted=engine.halted,
        max_drawdown=max_dd,
        engine=engine,
        base_rel=base_rel,
    )
