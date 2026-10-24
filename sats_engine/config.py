"""Strategy parameters. Every number here is a starting point to be
validated by backtest and shadow mode, not a known-good value.

Defaults follow the Hashira Beta v0.1.2 doctrine where it applies:
edge floor (2x fees and >=0.15% absolute), 72h hold cap, max 1 open
position, -10% drawdown halt, $500 capital. Fee rates here are backtest
estimates only; a live system must load them from the venue at runtime."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FeeModel:
    maker: float = 0.0010  # limit-order fee (fraction of notional)
    taker: float = 0.0020  # market-order fee
    slippage: float = 0.0005  # adverse move paid by marketable limit orders
    edge_fee_multiple: float = 2.0  # doctrine: net edge > 2x fees + slippage
    min_edge_abs: float = 0.0015  # doctrine: and >= 0.15% absolute

    def round_trip_cost(self) -> float:
        """Worst-case cost of a sell (taker) then a limit buyback (maker)."""
        return self.taker + self.slippage + self.maker

    def edge_floor(self) -> float:
        """Smallest price move (fraction) a round trip may target."""
        return max(self.edge_fee_multiple * self.round_trip_cost(), self.min_edge_abs)


@dataclass
class SwingParams:
    enabled: bool = True
    ema_period: int = 50
    trend_ema_period: int = 200
    atr_period: int = 14
    rsi_period: int = 14
    er_period: int = 30
    entry_z: float = 2.0  # sell when close is this many ATRs above the EMA
    entry_rsi: float = 68.0
    trend_er_block: float = 0.45  # ER above this in an uptrend = don't fade it
    target_atr: float = 2.5  # buyback target below the sell price
    stop_atr: float = 2.5  # sats-protection stop above the sell price
    max_hold_bars: int = 72  # doctrine hold cap: 72h on hourly bars
    size_frac: float = 0.25  # fraction of the sleeve per ticket
    max_open: int = 2


@dataclass
class ScalpParams:
    enabled: bool = True
    bb_period: int = 20
    bb_k: float = 2.2
    er_period: int = 20
    er_max: float = 0.30  # only scalp when the market is choppy/inefficient
    min_edge_multiple: float = 2.5  # expected move must beat N x round-trip fees
    stop_std: float = 1.5
    max_hold_bars: int = 12
    size_frac: float = 0.08
    max_open: int = 3


@dataclass
class OnChainParams:
    window: int = 24 * 7  # snapshots of history for z-scores
    w_fee: float = 0.40
    w_mempool: float = 0.30
    w_whale: float = 0.30
    hash_stress_dd: float = 0.15  # hashrate 15% below its rolling max = miner stress
    heat_size_boost: float = 0.5  # max extra sizing when heat confirms a signal


@dataclass
class DCAParams:
    contribution_usd: float = 0.0  # USD added every interval (0 disables)
    interval_bars: int = 24 * 7  # hours between contributions (converted to the bar size)
    min_mult: float = 0.5
    max_mult: float = 2.0


@dataclass
class EngineConfig:
    sleeve_frac: float = 0.30  # max fraction of the stack ever put at risk
    max_open_total: int = 1  # doctrine: max 1 open position across all legs
    halt_drawdown: float = 0.10  # doctrine: -10% from peak stack (in BTC) = HALT
    fees: FeeModel = field(default_factory=FeeModel)
    swing: SwingParams = field(default_factory=SwingParams)
    scalp: ScalpParams = field(default_factory=ScalpParams)
    onchain: OnChainParams = field(default_factory=OnChainParams)
    dca: DCAParams = field(default_factory=DCAParams)
    bar_seconds: int = 3600
