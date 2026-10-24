"""Walk-forward validation: the falsification test from STRATEGY.md.

Roll through history in folds. In each fold, pick the best parameters on a
training window, then score them on the next window they have never seen.
Only the unseen (out-of-sample) results count toward the verdict:

  PASS          mean out-of-sample edge vs HODL > 0 after fees, AND enough
                folds positive, AND enough round trips to mean anything
  FAIL          enough trips, but the edge isn't there
  INSUFFICIENT  too few trips or folds to judge - not a pass

A PASS is evidence for a council discussion, never a guarantee.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
from dataclasses import dataclass, field
from statistics import mean
from typing import Optional, Sequence

from . import backtest
from .config import EngineConfig
from .models import Bar, OnChainSnapshot

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT"


@dataclass
class ValidationParams:
    warmup_bars: int = 24 * 10  # > the 200-bar trend EMA, never scored
    train_bars: int = 24 * 90
    test_bars: int = 24 * 30
    min_folds: int = 3
    min_trips: int = 30  # out-of-sample round trips across all folds
    min_positive_frac: float = 0.6  # share of folds that must beat HODL
    # Kept deliberately small: a big grid finds noise that looks like edge.
    grid: dict = field(default_factory=lambda: {
        "swing.entry_z": [1.5, 2.0, 2.5],
        "swing.target_atr": [2.0, 3.0],
        "scalp.bb_k": [2.2, 2.8],
    })


def apply_overrides(base: EngineConfig, overrides: dict) -> EngineConfig:
    cfg = copy.deepcopy(base)
    for path, value in overrides.items():
        head, _, name = path.partition(".")
        target = cfg if head == "engine" else getattr(cfg, head)
        if not hasattr(target, name):
            raise ValueError(f"unknown parameter {path}")
        setattr(target, name, value)
    return cfg


def data_fingerprint(bars: Sequence[Bar]) -> str:
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.ts},{b.open},{b.high},{b.low},{b.close}\n".encode())
    return h.hexdigest()


def _window_snaps(snaps: Sequence[OnChainSnapshot], bars: Sequence[Bar], bar_seconds: int):
    lo, hi = bars[0].ts, bars[-1].ts + bar_seconds
    return [s for s in snaps if lo <= s.ts <= hi]


def walk_forward(
    bars: Sequence[Bar],
    snaps: Optional[Sequence[OnChainSnapshot]] = None,
    base: Optional[EngineConfig] = None,
    params: Optional[ValidationParams] = None,
) -> dict:
    base = base or EngineConfig()
    p = params or ValidationParams()
    snaps = sorted(snaps or (), key=lambda s: s.ts)
    keys = list(p.grid)
    candidates = [dict(zip(keys, combo)) for combo in itertools.product(*(p.grid[k] for k in keys))]

    folds = []
    start = 0
    while start + p.warmup_bars + p.train_bars + p.test_bars <= len(bars):
        train = bars[start: start + p.warmup_bars + p.train_bars]
        test = bars[start + p.train_bars: start + p.warmup_bars + p.train_bars + p.test_bars]
        train_snaps = _window_snaps(snaps, train, base.bar_seconds)
        test_snaps = _window_snaps(snaps, test, base.bar_seconds)

        best, best_edge = None, None
        for cand in candidates:
            res = backtest.run(train, train_snaps, apply_overrides(base, cand), score_from=p.warmup_bars)
            if best_edge is None or res.edge_vs_hodl > best_edge:
                best, best_edge = cand, res.edge_vs_hodl
        res = backtest.run(test, test_snaps, apply_overrides(base, best), score_from=p.warmup_bars)
        folds.append({
            "test_from": test[p.warmup_bars].ts,
            "test_to": test[-1].ts,
            "params": best,
            "train_edge": round(best_edge, 6),
            "test_edge": round(res.edge_vs_hodl, 6),
            "test_trips": res.trips,
            "test_max_drawdown": round(res.max_drawdown, 6),
        })
        start += p.test_bars

    trips = sum(f["test_trips"] for f in folds)
    edges = [f["test_edge"] for f in folds]
    positive = sum(e > 0 for e in edges) / len(edges) if edges else 0.0
    mean_edge = mean(edges) if edges else 0.0
    if len(folds) < p.min_folds or trips < p.min_trips:
        verdict = INSUFFICIENT
        why = f"{len(folds)} folds / {trips} out-of-sample trips (need {p.min_folds} / {p.min_trips})"
    elif mean_edge > 0 and positive >= p.min_positive_frac:
        verdict = PASS
        why = f"mean OOS edge {mean_edge * 100:+.3f}%, {positive:.0%} of folds beat HODL"
    else:
        verdict = FAIL
        why = f"mean OOS edge {mean_edge * 100:+.3f}%, {positive:.0%} of folds beat HODL"

    return {
        "schema_version": 1,
        "verdict": verdict,
        "why": why,
        "mean_test_edge": round(mean_edge, 6),
        "positive_fold_frac": round(positive, 4),
        "oos_trips": trips,
        "folds": folds,
        "data": {
            "bars": len(bars),
            "from": bars[0].ts if bars else None,
            "to": bars[-1].ts if bars else None,
            "sha256": data_fingerprint(bars),
            "onchain_snapshots": len(snaps),
        },
        "method": {
            "warmup_bars": p.warmup_bars, "train_bars": p.train_bars, "test_bars": p.test_bars,
            "min_folds": p.min_folds, "min_trips": p.min_trips, "min_positive_frac": p.min_positive_frac,
            "grid": p.grid,
        },
    }
