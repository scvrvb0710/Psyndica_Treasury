"""Direct Bitcoin node backend (Knots or Core JSON-RPC). Works on pruned nodes.

Reads the same OnChainSnapshot the mempool.space client produces, straight
from your own node - no mempool app, no electrs, no txindex:

    fee_fast        estimatesmartfee(1)
    mempool_*       getmempoolinfo
    block_fullness  getblockstats(total_weight) over recent blocks
    hashrate        getnetworkhashps over ~3 days of blocks
    whale_btc       new mempool txs since the last scan, decoded with
                    getrawtransaction (mempool txs need no txindex)

Whale flow here covers *every* new mempool tx, while mempool.space's
`/mempool/recent` only samples the latest few. Don't mix histories from
the two backends in one backtest - the z-scores would shift at the seam.

Auth comes from the node's cookie file or the BITCOIN_RPC_USER /
BITCOIN_RPC_PASSWORD environment variables. Credentials are never logged.
Keep RPC bound to 127.0.0.1 and run the scanner on the node itself.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import OnChainSnapshot
from .explorer import MAX_BLOCK_WEIGHT

DEFAULT_RPC_URL = "http://127.0.0.1:8332"
DEFAULT_COOKIE = "~/.bitcoin/.cookie"
HASHRATE_BLOCKS = 432  # ~3 days, matching the mempool.space reading

Transport = Callable[[Any], Any]


class RPCError(RuntimeError):
    pass


def load_auth(cookie_path: Optional[str] = DEFAULT_COOKIE) -> str:
    """Return "user:password" from env vars, else from the cookie file."""
    user, password = os.environ.get("BITCOIN_RPC_USER"), os.environ.get("BITCOIN_RPC_PASSWORD")
    if user and password:
        return f"{user}:{password}"
    if cookie_path:
        p = Path(cookie_path).expanduser()
        if p.exists():
            return p.read_text().strip()
    raise RPCError(
        "no RPC credentials: set BITCOIN_RPC_USER/BITCOIN_RPC_PASSWORD or point --rpc-cookie "
        "at the node's .cookie file"
    )


def http_transport(url: str, auth: str, timeout: float = 30.0) -> Transport:
    header = "Basic " + base64.b64encode(auth.encode()).decode()

    def send(payload: Any) -> Any:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": header, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    return send


class BitcoinRPC:
    def __init__(self, transport: Transport):
        self._send = transport
        self._id = 0

    def _req(self, method: str, params: list) -> dict:
        self._id += 1
        return {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params}

    def call(self, method: str, *params) -> Any:
        resp = self._send(self._req(method, list(params)))
        if resp.get("error"):
            raise RPCError(f"{method}: {resp['error']}")
        return resp["result"]

    def batch(self, method: str, param_sets: list[list]) -> list[Any]:
        """One HTTP round trip for many calls. Failed items come back as None
        (e.g. a tx that left the mempool between listing and fetching)."""
        if not param_sets:
            return []
        reqs = [self._req(method, ps) for ps in param_sets]
        by_id = {r.get("id"): r for r in self._send(reqs)}
        out = []
        for req in reqs:
            r = by_id.get(req["id"]) or {}
            out.append(None if r.get("error") else r.get("result"))
        return out


class NodeExplorer:
    """Snapshot source backed by your own node. Stateful: whale flow is the
    diff of the mempool between consecutive scans, so the first scan after
    start reports whale_btc=None while it records a baseline."""

    def __init__(
        self,
        rpc: BitcoinRPC,
        whale_threshold_btc: float = 100.0,
        fullness_blocks: int = 6,
        max_new_txs: int = 5000,
    ):
        self.rpc = rpc
        self.whale_threshold_btc = whale_threshold_btc
        self.fullness_blocks = fullness_blocks
        self.max_new_txs = max_new_txs
        self.status = "not scanned"
        self._seen: Optional[set[str]] = None

    # Individual readings ------------------------------------------------

    def fee_fast(self) -> Optional[float]:
        est = self.rpc.call("estimatesmartfee", 1)
        if "feerate" not in est:  # fresh node: not enough history to estimate
            return None
        return est["feerate"] * 1e8 / 1000  # BTC/kvB -> sat/vB

    def mempool(self) -> tuple[float, int]:
        info = self.rpc.call("getmempoolinfo")
        return float(info["bytes"]), int(info["size"])  # "bytes" is total vsize

    def block_fullness(self) -> float:
        tip = self.rpc.call("getblockcount")
        heights = [[h, ["total_weight"]] for h in range(tip, tip - self.fullness_blocks, -1)]
        stats = [s for s in self.rpc.batch("getblockstats", heights) if s]
        if not stats:
            raise RPCError("getblockstats returned nothing")
        return sum(s["total_weight"] / MAX_BLOCK_WEIGHT for s in stats) / len(stats)

    def hashrate(self) -> float:
        return float(self.rpc.call("getnetworkhashps", HASHRATE_BLOCKS)) / 1e18  # EH/s

    def whale_btc(self) -> Optional[float]:
        current = set(self.rpc.call("getrawmempool"))
        first_scan = self._seen is None
        new = [] if first_scan else sorted(current - self._seen)
        self._seen = current
        if first_scan:
            return None
        if len(new) > self.max_new_txs:
            # Bounded work per scan; sampling keeps the reading usable under load.
            step = len(new) / self.max_new_txs
            new = [new[int(i * step)] for i in range(self.max_new_txs)]
        total = 0.0
        for tx in self.rpc.batch("getrawtransaction", [[txid, True] for txid in new]):
            if not tx:
                continue
            btc = sum(v["value"] for v in tx["vout"])
            if btc >= self.whale_threshold_btc:
                total += btc
        return total

    # Composite ----------------------------------------------------------

    def snapshot(self) -> OnChainSnapshot:
        now = int(time.time())
        try:
            info = self.rpc.call("getblockchaininfo")
        except Exception as e:  # noqa: BLE001 - node down = stale, not neutral
            self.status = f"node unreachable: {type(e).__name__}"
            return OnChainSnapshot(ts=now)
        if info.get("initialblockdownload"):
            # Mempool and fee data mean nothing until the node has caught up.
            self.status = f"syncing {info.get('verificationprogress', 0) * 100:.1f}%"
            return OnChainSnapshot(ts=now)

        def safe(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - partial data beats no data
                return None

        mem = safe(self.mempool)
        snap = OnChainSnapshot(
            ts=now,
            fee_fast=safe(self.fee_fast),
            mempool_vsize=mem[0] if mem else None,
            mempool_count=mem[1] if mem else None,
            whale_btc=safe(self.whale_btc),
            block_fullness=safe(self.block_fullness),
            hashrate=safe(self.hashrate),
        )
        self.status = f"ok height={info.get('blocks')} pruned={info.get('pruned', False)}"
        return snap
