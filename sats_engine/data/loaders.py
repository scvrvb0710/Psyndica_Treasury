"""CSV loaders and writers for price bars and on-chain snapshots.

Price CSV columns:   ts,open,high,low,close[,volume]
On-chain CSV columns: ts[,fee_fast,mempool_vsize,mempool_count,whale_btc,block_fullness,hashrate]
`ts` is unix seconds. Empty cells become None.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, fields
from pathlib import Path
from typing import Iterable

from ..models import Bar, OnChainSnapshot


def load_bars(path: str | Path) -> list[Bar]:
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bars.append(
                Bar(
                    ts=int(float(row["ts"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def load_onchain(path: str | Path) -> list[OnChainSnapshot]:
    snaps = []
    names = {f.name: f.type for f in fields(OnChainSnapshot)}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            kw = {}
            for k, v in row.items():
                if k not in names or k == "ts":
                    continue
                if v in (None, ""):
                    kw[k] = None
                else:
                    kw[k] = int(float(v)) if k == "mempool_count" else float(v)
            snaps.append(OnChainSnapshot(ts=int(float(row["ts"])), **kw))
    snaps.sort(key=lambda s: s.ts)
    return snaps


def write_rows(path: str | Path, rows: Iterable, columns: list[str]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            d = asdict(r)
            w.writerow({c: ("" if d.get(c) is None else d[c]) for c in columns})


def append_snapshot(path: str | Path, snap: OnChainSnapshot) -> None:
    """Append one live snapshot, writing the header on first use."""
    columns = [f.name for f in fields(OnChainSnapshot)]
    path = Path(path)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if new:
            w.writeheader()
        d = asdict(snap)
        w.writerow({c: ("" if d[c] is None else d[c]) for c in columns})
