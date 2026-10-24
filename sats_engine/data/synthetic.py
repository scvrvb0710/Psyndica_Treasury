"""Synthetic BTC-like market with matching on-chain activity.

Used for tests and for exercising the engine offline. It has regime
switching (trend vs. chop), volatility clustering, and on-chain heat that
rises with realised volatility. Results on synthetic data say the code
works - they say nothing about whether the strategy makes money.
"""

from __future__ import annotations

import math
import random

from ..models import Bar, OnChainSnapshot


def generate(
    n_bars: int = 24 * 365,
    start_price: float = 60_000.0,
    bar_seconds: int = 3600,
    seed: int = 7,
    start_ts: int = 1_700_000_000,
) -> tuple[list[Bar], list[OnChainSnapshot]]:
    rng = random.Random(seed)
    bars: list[Bar] = []
    snaps: list[OnChainSnapshot] = []

    price = start_price
    vol = 0.006  # per-bar stdev
    regime = "chop"
    drift = 0.0
    anchor = price
    hashrate = 600.0

    for i in range(n_bars):
        if rng.random() < 1 / (24 * 10):
            regime = rng.choice(["trend_up", "trend_down", "chop", "chop"])
            drift = {"trend_up": 0.0006, "trend_down": -0.0005, "chop": 0.0}[regime]
            anchor = price

        # GARCH-ish vol clustering, bounded.
        shock = rng.gauss(0, 1)
        vol = min(0.03, max(0.002, 0.94 * vol + 0.06 * abs(shock) * 0.008))
        revert = -0.02 * math.log(price / anchor) if regime == "chop" else 0.0
        ret = drift + revert + vol * shock

        o = price
        c = price * math.exp(ret)
        wick = abs(rng.gauss(0, vol)) * price
        h = max(o, c) + wick * rng.random()
        l = min(o, c) - wick * rng.random()
        ts = start_ts + i * bar_seconds
        bars.append(Bar(ts, o, h, l, c, volume=rng.uniform(100, 1000) * (1 + 50 * vol)))
        price = c

        # On-chain heat tracks volatility plus noise; whales show up on big moves.
        stress = vol / 0.006
        hashrate *= math.exp(rng.gauss(0.0001, 0.004))
        snaps.append(
            OnChainSnapshot(
                ts=ts + bar_seconds,  # observed at bar close
                fee_fast=max(1.0, 8 * stress ** 1.5 * math.exp(rng.gauss(0, 0.3))),
                mempool_vsize=max(1e5, 4e7 * stress * math.exp(rng.gauss(0, 0.4))),
                mempool_count=int(max(100, 30_000 * stress * math.exp(rng.gauss(0, 0.3)))),
                whale_btc=max(0.0, rng.expovariate(1 / 300) * stress * (1 + 20 * abs(ret))),
                block_fullness=min(1.0, 0.85 + 0.05 * stress + rng.gauss(0, 0.03)),
                hashrate=hashrate,
            )
        )
    return bars, snaps
