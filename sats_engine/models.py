"""Core data types shared across the engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle. `ts` is unix seconds at bar open."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class OnChainSnapshot:
    """Block-explorer readings at a point in time.

    Any field may be None when the source did not provide it; the
    pressure model skips missing components rather than guessing.
    """

    ts: int
    fee_fast: Optional[float] = None  # sat/vB, next-block fee
    mempool_vsize: Optional[float] = None  # vbytes waiting
    mempool_count: Optional[int] = None  # tx count waiting
    whale_btc: Optional[float] = None  # BTC moved in large txs over the window
    block_fullness: Optional[float] = None  # 0..1, avg of recent blocks
    hashrate: Optional[float] = None  # EH/s


@dataclass
class Ticket:
    """A sats round trip: sell BTC now, buy it back later for more sats.

    The trade is only a success if `btc_bought_back > btc_sold`.
    """

    kind: str  # "swing" | "scalp"
    opened_ts: int
    sell_price: float
    btc_sold: float
    usd_held: float  # proceeds after fees, reserved for the buyback
    target: float  # limit buyback price (below sell_price)
    stop: float  # sats-protection stop (above sell_price)
    expires_ts: int
    closed_ts: Optional[int] = None
    buy_price: Optional[float] = None
    btc_bought_back: Optional[float] = None
    exit_reason: Optional[str] = None
    trace_id: str = field(default_factory=new_trace_id)

    @property
    def sats_delta(self) -> float:
        """BTC gained (positive) or lost (negative) by the round trip."""
        if self.btc_bought_back is None:
            return 0.0
        return self.btc_bought_back - self.btc_sold


@dataclass
class Fill:
    ts: int
    side: str  # "buy" | "sell"
    price: float
    btc: float
    usd: float
    fee_usd: float
    reason: str
    trace_id: str  # links every fill back to its ticket / DCA decision
    tags: dict = field(default_factory=dict)
