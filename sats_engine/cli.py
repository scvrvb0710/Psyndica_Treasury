"""Command line: demo | backtest | scan | collector-export | gov | validate | feed | terminal.

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
from . import feed as signal_feed
from . import terminal
from . import validate as wf
from .config import DCAParams, EngineConfig
from .data import synthetic
from .data.collector import CollectorClient, CollectorError, load_secret
from .data.explorer import DEFAULT_BASE_URL, MempoolExplorer
from .data.rpc import DEFAULT_COOKIE, DEFAULT_RPC_URL, BitcoinRPC, NodeExplorer, RPCError, http_transport, load_auth
from .data.loaders import DataError, append_snapshot, check_spacing, load_bars, load_onchain
from .timeframes import tf_name, tf_seconds
from .governance import cli as gov_cli
from .governance.ledger import LedgerError
from .governance.ledger import load as load_ledger
from .governance.sshsig import SignatureError, SSHVerifier
from .models import new_trace_id
from .signals.onchain import OnChainPressure

SHADOW_BANNER = "SHADOW MODE - research only, no orders are ever placed."


def _apply_base(cfg: EngineConfig, args, governed: bool) -> EngineConfig:
    """--base picks the bar size: 1h runs the single-timeframe engine, 15m the
    15m/4h/1d one. Under a ledger the bar size is governed (engine.bar_seconds)."""
    if args.base is None:
        return cfg
    want = tf_seconds(args.base)
    if governed and want != cfg.bar_seconds:
        raise LedgerError(f"ledger sets the base timeframe to {tf_name(cfg.bar_seconds)}; "
                          f"changing it needs a mandate (engine.bar_seconds)")
    cfg.bar_seconds = want
    return cfg


def _config(args) -> EngineConfig:
    if args.mandates:
        # Governed run: every parameter comes from the signed ledger, no overrides.
        if args.dca_usd or args.max_open is not None:
            raise LedgerError("--dca-usd/--max-open can't override a governed config; propose a mandate")
        state = load_ledger(args.mandates, SSHVerifier(), args.expect_genesis)
        args.capital_usd = state.params["capital_usd"]
        print(f"governed by ledger head {state.head[:16]} "
              f"({sum(r.applied for r in state.records)} mandates applied, mode {state.mode})")
        return _apply_base(state.config(), args, governed=True)
    cfg = _apply_base(EngineConfig(), args, governed=False)
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
    cfg = _config(args)
    n_bars = args.bars or 365 * 24 * 3600 // cfg.bar_seconds
    bars, snaps = synthetic.generate(n_bars=n_bars, seed=args.seed, bar_seconds=cfg.bar_seconds)
    initial_btc = args.capital_usd / bars[0].close
    res = backtest.run(bars, snaps, cfg, initial_btc=initial_btc)
    print("Synthetic market - proves the code runs, NOT that the strategy has edge.\n")
    _report(res, args.trades)
    return 0


def cmd_backtest(args) -> int:
    cfg = _config(args)
    bars, snaps = _load_checked(args, cfg)
    initial_btc = args.capital_usd / bars[0].close
    res = backtest.run(bars, snaps, cfg, initial_btc=initial_btc)
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
    collector = None
    if args.push:
        try:
            collector = CollectorClient(args.push, load_secret())
        except CollectorError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    pressure = OnChainPressure(EngineConfig().onchain)
    while True:
        snap = explorer.snapshot()
        reading = pressure.update(snap)
        status = getattr(explorer, "status", None)
        if all(v is None for k, v in asdict(snap).items() if k != "ts"):
            detail = f" ({status})" if status else ""
            print(f"WARNING: no data from {source}{detail} - treat as stale, not neutral.", file=sys.stderr)
        trace_id, source_kind = new_trace_id(), "rpc" if args.rpc else "mempool-api"
        record = {
            "trace_id": trace_id,
            "source": source_kind,
            "status": status,
            "snapshot": asdict(snap),
            "pressure": asdict(reading),
            "pressure_ready": reading.ready,
        }
        print(json.dumps(record))
        sys.stdout.flush()
        if args.save:
            append_snapshot(args.save, snap)
        if collector:
            try:
                collector.push(snap, trace_id, source_kind)
            except CollectorError as e:
                print(f"WARNING: {e}", file=sys.stderr)
            if collector.pending:
                print(f"WARNING: collector unreachable, {len(collector.pending)} snapshot(s) queued",
                      file=sys.stderr)
        if not args.watch:
            return 0
        time.sleep(args.interval)


def cmd_collector_export(args) -> int:
    try:
        client = CollectorClient(args.url, load_secret())
        csv_text = client.export_csv(args.source, args.since)
    except CollectorError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    with open(args.out, "w") as f:
        f.write(csv_text)
    print(f"wrote {max(csv_text.count(chr(10)) - 1, 0)} snapshots to {args.out}", file=sys.stderr)
    return 0


def _governed(args):
    """(config, capital_usd, governance state or None) for validate/feed."""
    if args.mandates:
        state = load_ledger(args.mandates, SSHVerifier(), args.expect_genesis)
        return _apply_base(state.config(), args, governed=True), state.params["capital_usd"], state
    return _apply_base(EngineConfig(), args, governed=False), args.capital_usd, None


def _load_checked(args, cfg: EngineConfig):
    bars = load_bars(args.prices)
    if not bars:
        raise DataError(f"no bars in {args.prices}")
    check_spacing(bars, cfg.bar_seconds)
    snaps = load_onchain(args.onchain) if args.onchain else []
    return bars, snaps


def cmd_validate(args) -> int:
    cfg, _, _ = _governed(args)
    bars, snaps = _load_checked(args, cfg)
    report = wf.walk_forward(bars, snaps, cfg)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(SHADOW_BANNER)
    print(f"base timeframe {report['method']['base_timeframe']}, "
          f"warm-up {report['method']['warmup_hours']}h")
    print(f"verdict: {report['verdict']} - {report['why']}")
    for fold in report["folds"]:
        print(f"  test {fold['test_from']}..{fold['test_to']}  train {fold['train_edge'] * 100:+.2f}%  "
              f"test {fold['test_edge'] * 100:+.2f}%  trips {fold['test_trips']}")
    print(f"wrote {args.out}")
    return 0


def _feed_once(args) -> int:
    cfg, capital, gov = _governed(args)
    bars, snaps = _load_checked(args, cfg)
    validation = json.loads(open(args.validation).read()) if args.validation else None
    feed = signal_feed.SignalFeed(cfg, capital / bars[0].close, gov, validation)
    writer = signal_feed.FeedWriter(args.out)
    return sum(writer.write(body) is not None for body in signal_feed.run(feed, bars, snaps))


def cmd_feed_run(args) -> int:
    print(SHADOW_BANNER, file=sys.stderr)
    while True:
        n = _feed_once(args)
        print(f"appended {n} record(s) to {args.out}", file=sys.stderr)
        if not args.follow:
            return 0
        time.sleep(args.interval)


def cmd_feed_verify(args) -> int:
    try:
        n = signal_feed.verify(args.path)
    except signal_feed.FeedError as e:
        print(f"BROKEN: {e}", file=sys.stderr)
        return 1
    print(f"ok: {n} records, chain intact")
    return 0


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
        p.add_argument("--mandates", metavar="DIR", help="take every parameter from the signed ledger in DIR")
        p.add_argument("--expect-genesis", help="pinned genesis hash for --mandates")
        p.add_argument("--base", choices=["1h", "15m"],
                       help="bar size: 1h single-timeframe, 15m = 15m scalp / 4h swing / 1d trend filter")

    p = sub.add_parser("demo", help="run on a synthetic market")
    common(p)
    p.add_argument("--bars", type=int, default=None, help="default: one year of bars")
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
    p.add_argument("--push", metavar="URL",
                   help="also push signed snapshots to the Cloudflare collector (secret from SATS_COLLECTOR_SECRET)")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("collector-export", help="download collected snapshots as a backtest CSV")
    p.add_argument("--url", required=True, help="collector base URL (https://...)")
    p.add_argument("--source", default="rpc", choices=["rpc", "mempool-api"])
    p.add_argument("--since", type=int, default=0, help="unix seconds")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_collector_export)

    def governed(p):
        p.add_argument("--mandates", metavar="DIR", help="take parameters from the signed ledger in DIR")
        p.add_argument("--expect-genesis", help="pinned genesis hash for --mandates")
        p.add_argument("--capital-usd", type=float, default=500.0, help="ignored with --mandates")
        p.add_argument("--base", choices=["1h", "15m"],
                       help="bar size: 1h single-timeframe, 15m = 15m scalp / 4h swing / 1d trend filter")

    p = sub.add_parser("validate", help="walk-forward test: does the edge survive unseen data?")
    governed(p)
    p.add_argument("--prices", required=True)
    p.add_argument("--onchain")
    p.add_argument("--out", default="validation.json")
    p.set_defaults(fn=cmd_validate)

    feed_p = sub.add_parser("feed", help="the unified signal feed (one hash-chained record per bar)")
    fsub = feed_p.add_subparsers(dest="feed_cmd", required=True)
    p = fsub.add_parser("run", help="append records for any new bars")
    governed(p)
    p.add_argument("--prices", required=True, help="hourly bars CSV (keep appending to it for live use)")
    p.add_argument("--onchain", help="snapshots CSV, e.g. from scan --save")
    p.add_argument("--validation", help="report from `validate`")
    p.add_argument("--out", default="feed.jsonl")
    p.add_argument("--follow", action="store_true", help="keep running, picking up new bars")
    p.add_argument("--interval", type=float, default=60.0)
    p.set_defaults(fn=cmd_feed_run)
    p = fsub.add_parser("verify", help="check the hash chain of a feed file")
    p.add_argument("path")
    p.set_defaults(fn=cmd_feed_verify)

    p = sub.add_parser("terminal", help="live read-only dashboard of a feed file")
    p.add_argument("path", nargs="?", default="feed.jsonl")
    p.add_argument("--follow", "-f", action="store_true", help="redraw as new records arrive")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--no-verify", action="store_true", help="skip the chain check at startup")
    p.set_defaults(fn=lambda a: terminal.run(a.path, a.follow, a.interval, not a.no_verify))

    gov_cli.register(sub)

    args = ap.parse_args(argv)
    if args.cmd == "gov":
        return gov_cli.run(args)
    try:
        return args.fn(args)
    except (LedgerError, SignatureError, signal_feed.FeedError, DataError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
