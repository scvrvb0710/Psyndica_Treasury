"""The Sats Ratchet: a BTC-denominated accumulation engine (research / shadow).

BTC stores. The accounting unit is BTC, not USD. The stack is split into:
  * core   - never sold, ever.
  * sleeve - up to `sleeve_frac` of the stack, lent out to round trips.
Every sell opens a Ticket whose USD is ring-fenced for buying the same
position back. A ticket closes by one of:
  * target - passive limit buyback below the sell price (more sats),
  * stop   - price ran away; marketable limit buyback caps the sats lost,
  * expiry - hold cap reached; marketable limit buyback at the close.
The engine never holds open-ended USD exposure: the worst case is a bounded
sats loss per ticket, never being priced out of the stack.

Every order is modelled as a limit order. Passive limits pay the maker fee;
marketable limits (sells into strength, stops, expiries) pay taker fee plus
slippage. There is no execution code here - this class only simulates.

A drawdown of `halt_drawdown` from the peak stack (in BTC) halts new
tickets. Open tickets still exit on their own rules. A halt is sticky:
only `resume()` - a human decision - clears it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import EngineConfig
from ..models import Bar, Fill, OnChainSnapshot, Ticket, new_trace_id
from ..signals.onchain import OnChainPressure, PressureReading
from .scalp import ScalpSignal
from .swing import SwingSignal, TicketPlan


HOLD_CAP_S = 72 * 3600  # doctrine: no ticket stays open longer than 72h, on any timeframe


@dataclass
class Exposure:
    btc: float
    btc_in_tickets: float
    usd_in_tickets: float
    usd_reserve: float


class SatsRatchet:
    def __init__(self, config: EngineConfig, initial_btc: float):
        self.cfg = config
        self.btc = initial_btc
        self.usd_reserve = 0.0  # DCA cash not yet deployed
        self.open: list[Ticket] = []
        self.closed: list[Ticket] = []
        self.fills: list[Fill] = []
        self.decisions: list[dict] = []  # skipped signals, halts - the shadow log
        self.pressure = OnChainPressure(config.onchain)
        boost = config.onchain.heat_size_boost
        self.swing = SwingSignal(config.swing, config.fees, boost)
        self.scalp = ScalpSignal(config.scalp, config.fees, boost)
        self.peak_stack_btc = initial_btc
        self.halted = False
        self._bar_index = 0
        # Bar length each leg's signals are measured in (multi-timeframe
        # engines override this); hold limits are converted to seconds.
        self.leg_seconds = {"swing": config.bar_seconds, "scalp": config.bar_seconds}
        # DCA interval is configured in hours; convert to base bars.
        self.dca_every = max(1, round(config.dca.interval_bars * 3600 / config.bar_seconds))

    # ------------------------------------------------------------------ state

    def exposure(self) -> Exposure:
        return Exposure(
            btc=self.btc,
            btc_in_tickets=sum(t.btc_sold for t in self.open),
            usd_in_tickets=sum(t.usd_held for t in self.open),
            usd_reserve=self.usd_reserve,
        )

    def stack_btc(self, price: float) -> float:
        """Everything owned, valued in BTC at `price`."""
        e = self.exposure()
        return e.btc + (e.usd_in_tickets + e.usd_reserve) / price

    def resume(self) -> None:
        """Clear a drawdown halt. Call only after a human review."""
        self.halted = False

    def _log(self, bar: Bar, event: str, **detail) -> None:
        self.decisions.append({"ts": bar.ts, "event": event, "trace_id": new_trace_id(), **detail})

    # --------------------------------------------------------------- per bar

    def on_bar(self, bar: Bar, snaps: Optional[list[OnChainSnapshot]] = None) -> list[TicketPlan]:
        """Advance one bar. Returns the signals that fired (taken or not)."""
        for snap in snaps or ():
            self.pressure.update(snap)
        pressure = self.pressure.last

        self._manage_tickets(bar)
        self._check_halt(bar)

        # Always update both legs so their indicators stay warm.
        plans = [self.swing.update(bar, pressure), self.scalp.update(bar, pressure)]
        for plan in plans:
            if plan is not None:
                self._open_ticket(bar, plan)

        self._dca(bar, pressure)
        self._bar_index += 1
        return [p for p in plans if p is not None]

    def _check_halt(self, bar: Bar) -> None:
        # Valued at the close: a conservative mark that ignores intrabar recovery.
        stack = self.stack_btc(bar.close)
        self.peak_stack_btc = max(self.peak_stack_btc, stack)
        drawdown = 1 - stack / self.peak_stack_btc
        if not self.halted and drawdown >= self.cfg.halt_drawdown:
            self.halted = True
            self._log(bar, "halt", drawdown=round(drawdown, 4), stack_btc=stack)

    # -------------------------------------------------------------- tickets

    def _manage_tickets(self, bar: Bar) -> None:
        fees = self.cfg.fees
        still_open = []
        for t in self.open:
            # Gaps fill at the open; if both levels trade inside one bar we
            # assume the stop hit first (pessimistic, avoids flattering results).
            if bar.open >= t.stop:
                self._close(t, bar, bar.open * (1 + fees.slippage), fees.taker, "stop-gap")
            elif bar.high >= t.stop:
                self._close(t, bar, t.stop * (1 + fees.slippage), fees.taker, "stop")
            elif bar.open <= t.target:
                self._close(t, bar, bar.open, fees.maker, "target-gap")
            elif bar.low <= t.target:
                self._close(t, bar, t.target, fees.maker, "target")
            elif bar.ts >= t.expires_ts:
                self._close(t, bar, bar.close * (1 + fees.slippage), fees.taker, "expiry")
            else:
                still_open.append(t)
        self.open = still_open

    def _close(self, t: Ticket, bar: Bar, price: float, fee_rate: float, reason: str) -> None:
        fee_usd = t.usd_held * fee_rate
        btc = (t.usd_held - fee_usd) / price
        self.btc += btc
        t.closed_ts, t.buy_price, t.btc_bought_back, t.exit_reason = bar.ts, price, btc, reason
        self.closed.append(t)
        self.fills.append(
            Fill(bar.ts, "buy", price, btc, t.usd_held, fee_usd, f"{t.kind}:{reason}", t.trace_id)
        )

    def _open_ticket(self, bar: Bar, plan: TicketPlan) -> None:
        leg = self.cfg.swing if plan.kind == "swing" else self.cfg.scalp
        if self.halted:
            self._log(bar, "skip", kind=plan.kind, why="halted", signal=plan.reason)
            return
        if len(self.open) >= self.cfg.max_open_total:
            return
        if sum(1 for t in self.open if t.kind == plan.kind) >= leg.max_open:
            return

        stack = self.btc + sum(t.btc_sold for t in self.open)
        sleeve_cap = self.cfg.sleeve_frac * stack
        available = sleeve_cap - sum(t.btc_sold for t in self.open)
        size = min(leg.size_frac * sleeve_cap * plan.size_mult, available, self.btc)
        if size <= 1e-8:
            return

        fees = self.cfg.fees
        price = bar.close * (1 - fees.slippage)
        gross = size * price
        fee_usd = gross * fees.taker
        self.btc -= size
        ticket = Ticket(
            kind=plan.kind,
            opened_ts=bar.ts,
            sell_price=price,
            btc_sold=size,
            usd_held=gross - fee_usd,
            target=plan.target,
            stop=plan.stop,
            expires_ts=bar.ts + min(plan.max_hold_bars * self.leg_seconds[plan.kind], HOLD_CAP_S),
        )
        self.open.append(ticket)
        self.fills.append(
            Fill(
                bar.ts, "sell", price, size, gross, fee_usd, f"{plan.kind}:open",
                ticket.trace_id, {"why": plan.reason},
            )
        )

    # ------------------------------------------------------------------ DCA

    def dca_multiplier(self, pressure: PressureReading) -> float:
        """Buy more when price is stretched below trend, more again if the chain
        shows panic (heat while falling) or miner capitulation. Nerve in
        dislocation, discipline in euphoria - bounded by min/max_mult."""
        d = self.cfg.dca
        ema, atr = self.swing.ema.value, self.swing.atr.value
        last_close = self.swing.last_close
        if ema is None or atr is None or not atr or last_close is None:
            return 1.0
        stretch = (last_close - ema) / atr
        score = max(-1.0, min(1.0, -stretch / 2.0))
        if stretch < 0:
            score += 0.5 * max(pressure.heat, 0.0)
        if pressure.miner_stress:
            score += 0.5
        return max(d.min_mult, min(d.max_mult, 1.0 + score))

    def dca_due(self, bar_index: int) -> bool:
        return self.cfg.dca.contribution_usd > 0 and bar_index % self.dca_every == 0

    def _dca(self, bar: Bar, pressure: PressureReading) -> None:
        d = self.cfg.dca
        if not self.dca_due(self._bar_index):
            return
        self.usd_reserve += d.contribution_usd
        spend = min(self.usd_reserve, d.contribution_usd * self.dca_multiplier(pressure))
        if spend <= 0:
            return
        fees = self.cfg.fees
        price = bar.close * (1 + fees.slippage)
        fee_usd = spend * fees.taker
        btc = (spend - fee_usd) / price
        self.usd_reserve -= spend
        self.btc += btc
        self.fills.append(Fill(bar.ts, "buy", price, btc, spend, fee_usd, "dca", new_trace_id()))
