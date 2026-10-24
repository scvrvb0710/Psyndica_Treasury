"""Command line: demo | backtest | scan.

Nothing here places orders. `scan` is shadow mode: it reads the block
explorer, scores on-chain pressure, and logs the reading with a trace_id.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from . import backtest
from .config import DCAParams, EngineConfig
from .data import synthetic
from .data.explorer import DEFAULT_BASE_URL, MempoolExplorer
from .data.rpc import DEFAULT_COOKIE, DEFAULT_RPC_URL, BitcoinRPC, NodeExplorer, RPCError, http_transport, load_auth
from .data.loaders import append_snapshot, load_bars, load_onchain
from .models import new_trace_id
from .signals.onchain import OnChainPressure

SHADOW_BANNER = "SHADOW MODE - research only, no orders are ever placed."


def _config(args) -> EngineConfig:
    cfg = EngineConfig()
    if args.dca_usd:
        cfg.dca = DCAParams(contribution_usd=args.dca_usd)
    if args.max_open is not None:
        cfg.max_open_total = args.max_open
    return cfg


def _report(res: backtest.BacktestResult, show_trades: int) -> None:
    print(SHADOW_BANNER)
    print(res.summary())
    if show_trades:
        print("\nlast tickets:")
        for t in res.engine.closed[-show_trades:]:
            print(
                f"  {t.trace_id} {t.kind:<5} sell@{t.sell_price:,.0f} buy@{t.buy_price:,.0f} "
                f"{t.exit_reason:<10} {t.sats_delta * 1e8:+,.0f} sats"
            )


def cmd_demo(args) -> int:
    bars, snaps = synthetic.generate(n_bars=args.bars, seed=args.seed)
    initial_btc = args.capital_usd / bars[0].close
    res = backtest.run(bars, snaps, _config(args), initial_btc=initial_btc)
    print("Synthetic market - proves the code runs, NOT that the strategy has edge.\n")
    _report(res, args.trades)
    return 0


def cmd_backtest(args) -> int:
    bars = load_bars(args.prices)
    snaps = load_onchain(args.onchain) if args.onchain else []
    initial_btc = args.capital_usd / bars[0].close
    res = backtest.run(bars, snaps, _config(args), initial_btc=initial_btc)
    _report(res, args.trades)
    return 0


def cmd_scan(args) -> int:
    print(SHADOW_BANNER, file=sys.stderr)
    if args.rpc:
        try:
            auth = load_auth(args.rpc_cookie)
        except RPCError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        rpc = BitcoinRPC(http_transport(args.rpc_url, auth))
        explorer = NodeExplorer(rpc, whale_threshold_btc=args.whale_btc)
        source = args.rpc_url
    else:
        explorer = MempoolExplorer(args.base_url, whale_threshold_btc=args.whale_btc)
        source = args.base_url
    pressure = OnChainPressure(EngineConfig().onchain)
    while True:
        snap = explorer.snapshot()
        reading = pressure.update(snap)
        status = getattr(explorer, "status", None)
        if all(v is None for k, v in asdict(snap).items() if k != "ts"):
            detail = f" ({status})" if status else ""
            print(f"WARNING: no data from {source}{detail} - treat as stale, not neutral.", file=sys.stderr)
        record = {
            "trace_id": new_trace_id(),
            "source": "rpc" if args.rpc else "mempool-api",
            "status": status,
            "snapshot": asdict(snap),
            "pressure": asdict(reading),
            "pressure_ready": reading.ready,
        }
        print(json.dumps(record))
        sys.stdout.flush()
        if args.save:
            append_snapshot(args.save, snap)
        if not args.watch:
            return 0
        time.sleep(args.interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sats_engine", description=SHADOW_BANNER)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--capital-usd", type=float, default=500.0,
                       help="starting stack in USD, converted to BTC at the first bar (default: 500)")
        p.add_argument("--dca-usd", type=float, default=0.0, help="weekly USD contribution")
        p.add_argument("--max-open", type=int, default=None,
                       help="override the 1-open-position doctrine cap (research only)")
        p.add_argument("--trades", type=int, default=10, help="print the last N tickets")

    p = sub.add_parser("demo", help="run on a synthetic market")
    common(p)
    p.add_argument("--bars", type=int, default=24 * 365)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("backtest", help="run on CSV data")
    common(p)
    p.add_argument("--prices", required=True, help="CSV: ts,open,high,low,close[,volume]")
    p.add_argument("--onchain", help="CSV of on-chain snapshots (see data/loaders.py)")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("scan", help="read the block explorer and score on-chain pressure")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help="mempool.space-compatible API; prefer your own node")
    p.add_argument("--rpc", action="store_true",
                   help="read your own Knots/Core node over JSON-RPC instead (pruned is fine)")
    p.add_argument("--rpc-url", default=DEFAULT_RPC_URL)
    p.add_argument("--rpc-cookie", default=DEFAULT_COOKIE,
                   help="node .cookie file; or set BITCOIN_RPC_USER/BITCOIN_RPC_PASSWORD")
    p.add_argument("--whale-btc", type=float, default=100.0)
    p.add_argument("--watch", action="store_true", help="loop forever")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between scans")
    p.add_argument("--save", help="append snapshots to this CSV for later backtests")
    p.set_defaults(fn=cmd_scan)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
