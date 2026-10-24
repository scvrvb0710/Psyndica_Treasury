"""The Psyndica signal feed: one algorithm, one record per bar.

Every closed bar runs the whole stack - market regime, on-chain pressure,
both strategy legs, smart DCA, the simulated sats stack, governance state,
and the walk-forward verdict - and emits one record to an append-only
JSON Lines feed. Internal consumers (dashboards, agents, alerts) read the
feed; none of them need to know how the parts work.

The feed never instructs anyone to trade. Signals are `would_sell` /
`would_rebuy` in shadow mode, and `posture` sums up how much weight a
consumer should give them:

  halted              governance or the engine halted: ignore signals
  stale_data          on-chain data is missing or old: signals are partial
  validation_failed   walk-forward says these signals lose to HODL
  unvalidated_shadow  no walk-forward verdict behind these signals
  validated_shadow    walk-forward PASS, still shadow mode
  live_eligible       PASS + the council voted go_live + fresh data

Records are hash-chained (prev_hash -> hash) so tampering or gaps are
detectable with `feed verify`. Replaying the same inputs yields the same
records, so a restart resumes the chain without duplicates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .config import EngineConfig
from .governance.ledger import State as GovState
from .governance.ledger import canonical, digest
from .models import Bar, OnChainSnapshot
from .strategy.accumulator import SatsRatchet
from .strategy.multitf import MultiTimeframeRatchet, engine_class
from .timeframes import tf_name

SCHEMA_VERSION = 2  # v2: per-timeframe block + higher-timeframe blocking
GENESIS_PREV = "0" * 64
STALE_AFTER_S = 2 * 3600


class FeedError(RuntimeError):
    pass


def _r(x: Optional[float], nd: int) -> Optional[float]:
    return None if x is None else round(x, nd)


def _tf_block(close, regime, ema, trend, atr, rsi, er, bar_ts=None) -> dict:
    return {
        "bar_ts": bar_ts, "close": _r(close, 2), "regime": regime, "ema": _r(ema, 2),
        "trend_ema": _r(trend, 2), "atr": _r(atr, 2), "rsi": _r(rsi, 2), "efficiency": _r(er, 4),
    }


class SignalFeed:
    def __init__(
        self,
        config: EngineConfig,
        initial_btc: float,
        governance: Optional[GovState] = None,
        validation: Optional[dict] = None,
        stale_after_s: int = STALE_AFTER_S,
    ):
        self.cfg = config
        self.engine: SatsRatchet = engine_class(config)(config, initial_btc)
        self.governance = governance
        self.validation = validation
        self.stale_after_s = stale_after_s
        self.hodl = initial_btc
        self.last_snap_ts: Optional[int] = None
        self._i = 0
        if governance and governance.halted:
            self.engine.halted = True

    def _regime(self, close: float) -> str:
        sw = self.engine.swing
        er, trend = sw.er.value, sw.trend.value
        if er is None or trend is None:
            return "warming_up"
        if er > self.cfg.swing.trend_er_block:
            return "trending_up" if close > trend else "trending_down"
        return "choppy"

    def _timeframes(self, bar: Bar) -> Optional[dict]:
        """Per-timeframe view for the multi-timeframe engine; None otherwise."""
        eng = self.engine
        if not isinstance(eng, MultiTimeframeRatchet):
            return None
        sc, sw, d = eng.scalp, eng.swing, eng.daily
        bb_z = (bar.close - sc.bb.mean) / sc.bb.std if sc.bb.ready and sc.bb.std > 0 else None
        b4 = eng.last_swing_bar
        return {
            "15m": {"close": _r(bar.close, 2), "bb_z": _r(bb_z, 3), "efficiency": _r(sc.er.value, 4)},
            "4h": _tf_block(b4.close if b4 else None, self._regime(b4.close) if b4 else "warming_up",
                            sw.ema.value, sw.trend.value, sw.atr.value, sw.rsi.value, sw.er.value,
                            b4.ts if b4 else None),
            "1d": {"close": _r(d.close, 2), "regime": d.regime, "ema": _r(d.ema.value, 2),
                   "efficiency": _r(d.er.value, 4)},
        }

    def step(self, bar: Bar, snaps: Sequence[OnChainSnapshot] = ()) -> dict:
        """Process one closed bar and return the record body (unchained)."""
        for s in snaps:
            self.last_snap_ts = max(self.last_snap_ts or s.ts, s.ts)
        eng = self.engine
        closed_before = len(eng.closed)
        plans = eng.on_bar(bar, list(snaps))

        if eng.dca_due(self._i):
            price = bar.close * (1 + self.cfg.fees.slippage)
            self.hodl += self.cfg.dca.contribution_usd * (1 - self.cfg.fees.taker) / price
        self._i += 1

        close_ts = bar.ts + self.cfg.bar_seconds
        age = close_ts - self.last_snap_ts if self.last_snap_ts is not None else None
        fresh = age is not None and age <= self.stale_after_s
        pr = eng.pressure.last
        sw = eng.swing

        blocked = getattr(eng, "blocked", [])
        signals = []
        for p in plans:
            taken = any(t.kind == p.kind and t.opened_ts == bar.ts for t in eng.open)
            signals.append({
                "leg": p.kind, "action": "would_sell", "taken_in_sim": taken,
                "blocked_by_htf": p in blocked,
                "target": _r(p.target, 2), "stop": _r(p.stop, 2),
                "size_mult": _r(p.size_mult, 4), "why": p.reason,
            })
        for t in eng.closed[closed_before:]:
            signals.append({
                "leg": t.kind, "action": "would_rebuy", "exit": t.exit_reason,
                "price": _r(t.buy_price, 2), "sats_delta": round(t.sats_delta * 1e8),
            })

        stack = eng.stack_btc(bar.close)
        gov = self.governance
        val = self.validation
        halted = eng.halted or bool(gov and gov.halted)
        validated = bool(val and val.get("verdict") == "PASS")
        if halted:
            posture = "halted"
        elif not fresh:
            posture = "stale_data"
        elif val and val.get("verdict") == "FAIL":
            posture = "validation_failed"
        elif not validated:
            posture = "unvalidated_shadow"
        elif gov and gov.mode == "live":
            posture = "live_eligible"
        else:
            posture = "validated_shadow"

        return {
            "schema_version": SCHEMA_VERSION,
            "ts": close_ts,
            "posture": posture,
            "market": {
                "close": _r(bar.close, 2),
                "regime": self._regime(bar.close),
                "ema": _r(sw.ema.value, 2), "trend_ema": _r(sw.trend.value, 2),
                "atr": _r(sw.atr.value, 2), "rsi": _r(sw.rsi.value, 2),
                "efficiency": _r(sw.er.value, 4),
            },
            "onchain": {
                "fresh": fresh, "age_s": age, "heat": _r(pr.heat, 4),
                "fee_z": _r(pr.fee_z, 3), "mempool_z": _r(pr.mempool_z, 3), "whale_z": _r(pr.whale_z, 3),
                "miner_stress": pr.miner_stress,
            },
            "base_timeframe": tf_name(self.cfg.bar_seconds),
            "timeframes": self._timeframes(bar),
            "signals": signals,
            "dca_multiplier": _r(eng.dca_multiplier(pr), 4),
            "stack": {
                "btc": _r(stack, 8), "hodl_btc": _r(self.hodl, 8),
                "edge_vs_hodl": _r(stack / self.hodl - 1, 6),
                "open_tickets": len(eng.open), "engine_halted": eng.halted,
            },
            "governance": None if gov is None else {
                "mode": gov.mode, "halted": gov.halted, "review_required": gov.review_required,
                "ledger_head": gov.head, "genesis": gov.genesis_hash,
            },
            "validation": None if val is None else {
                "verdict": val.get("verdict"), "mean_test_edge": val.get("mean_test_edge"),
                "oos_trips": val.get("oos_trips"), "data_to": val.get("data", {}).get("to"),
            },
        }


def run(
    feed: SignalFeed,
    bars: Sequence[Bar],
    snaps: Sequence[OnChainSnapshot] = (),
) -> Iterable[dict]:
    """Replay bars in order, yielding one record body per bar (no lookahead)."""
    snaps = sorted(snaps, key=lambda s: s.ts)
    j = 0
    for bar in bars:
        close_ts = bar.ts + feed.cfg.bar_seconds
        batch = []
        while j < len(snaps) and snaps[j].ts <= close_ts:
            batch.append(snaps[j])
            j += 1
        yield feed.step(bar, batch)


class FeedWriter:
    """Append-only, hash-chained JSON Lines. Resumes from the last record."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.seq, self.prev, self.last_ts = 0, GENESIS_PREV, None
        if self.path.exists() and self.path.stat().st_size:
            last = self.path.read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1]
            rec = json.loads(last)
            self.seq, self.prev, self.last_ts = rec["seq"], rec["hash"], rec["ts"]

    def write(self, body: dict) -> Optional[dict]:
        """Chain and append one record; skip bars already in the feed."""
        if self.last_ts is not None and body["ts"] <= self.last_ts:
            return None
        rec = {**body, "seq": self.seq + 1, "prev_hash": self.prev}
        rec["hash"] = digest(rec)
        rec["trace_id"] = rec["hash"][:16]
        with open(self.path, "ab") as f:
            f.write(canonical(rec) + b"\n")
        self.seq, self.prev, self.last_ts = rec["seq"], rec["hash"], rec["ts"]
        return rec


def verify(path: str | Path) -> int:
    """Check the whole chain. Returns the record count or raises FeedError."""
    prev, seq = GENESIS_PREV, 0
    with open(path, "rb") as f:
        for n, line in enumerate(f, start=1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise FeedError(f"line {n}: invalid JSON") from e
            body = {k: v for k, v in rec.items() if k not in ("hash", "trace_id")}
            if rec.get("seq") != seq + 1:
                raise FeedError(f"line {n}: seq {rec.get('seq')} after {seq} (gap or reorder)")
            if rec.get("prev_hash") != prev:
                raise FeedError(f"line {n}: prev_hash does not link to the previous record")
            if digest(body) != rec.get("hash") or rec.get("trace_id") != rec["hash"][:16]:
                raise FeedError(f"line {n}: hash mismatch (record was altered)")
            prev, seq = rec["hash"], rec["seq"]
    return seq
