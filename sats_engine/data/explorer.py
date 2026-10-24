"""Block-explorer client for any mempool.space-compatible REST API.

Point `base_url` at your own node (e.g. a self-hosted mempool/electrs
instance on Umbrel or Start9) to avoid leaking what you watch to a third
party and to dodge public rate limits.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..models import OnChainSnapshot

DEFAULT_BASE_URL = "https://mempool.space/api"
MAX_BLOCK_WEIGHT = 4_000_000
SATS_PER_BTC = 100_000_000

Fetcher = Callable[[str], Any]


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "sats-engine/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


@dataclass
class WhaleTx:
    txid: str
    btc: float
    fee_rate: float


class MempoolExplorer:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        whale_threshold_btc: float = 100.0,
        fetch: Optional[Fetcher] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.whale_threshold_btc = whale_threshold_btc
        self._fetch = fetch or _http_get_json

    def _get(self, path: str) -> Any:
        return self._fetch(f"{self.base_url}{path}")

    # Individual readings ------------------------------------------------

    def fee_fast(self) -> float:
        return float(self._get("/v1/fees/recommended")["fastestFee"])

    def mempool(self) -> tuple[float, int]:
        m = self._get("/mempool")
        return float(m["vsize"]), int(m["count"])

    def block_fullness(self, n: int = 6) -> float:
        blocks = self._get("/v1/blocks")[:n]
        if not blocks:
            raise ValueError("explorer returned no blocks")
        return sum(b["weight"] / MAX_BLOCK_WEIGHT for b in blocks) / len(blocks)

    def whale_txs(self) -> list[WhaleTx]:
        """Large transactions among the most recent mempool entries.

        `/mempool/recent` returns only the latest few entries, so call this
        on a short loop (see `scan --watch`) to build up a whale-flow series.
        """
        out = []
        for tx in self._get("/mempool/recent"):
            btc = tx["value"] / SATS_PER_BTC
            if btc >= self.whale_threshold_btc:
                vsize = tx.get("vsize") or 1
                out.append(WhaleTx(tx["txid"], btc, tx.get("fee", 0) / vsize))
        return out

    def hashrate(self) -> float:
        """Current network hashrate in EH/s."""
        data = self._get("/v1/mining/hashrate/3d")
        return float(data["currentHashrate"]) / 1e18

    # Composite ----------------------------------------------------------

    def snapshot(self) -> OnChainSnapshot:
        """Pull every reading; a failing endpoint leaves its field as None."""

        def safe(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - partial data beats no data
                return None

        mem = safe(self.mempool)
        whales = safe(self.whale_txs)
        return OnChainSnapshot(
            ts=int(time.time()),
            fee_fast=safe(self.fee_fast),
            mempool_vsize=mem[0] if mem else None,
            mempool_count=mem[1] if mem else None,
            whale_btc=sum(w.btc for w in whales) if whales is not None else None,
            block_fullness=safe(self.block_fullness),
            hashrate=safe(self.hashrate),
        )
