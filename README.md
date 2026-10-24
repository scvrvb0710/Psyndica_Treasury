# Psyndica Treasury · Sats Ratchet

A BTC-denominated swing and scalp **research engine** that uses block-explorer
(mempool) pressure signals. It measures success in sats, not dollars.

> **Shadow mode only.** Nothing in this repo places orders. Read
> [STRATEGY.md](STRATEGY.md) for the thesis, the red-team case against it,
> and the path to a council vote. Agents: [CLAUDE.md](CLAUDE.md) holds the
> Psyndica Ethos and the hard rules.

## Quick start

Stdlib-only Python 3.10+. Nothing to install.

```bash
python3 -m sats_engine demo                     # synthetic market, $500 stack
python3 -m sats_engine demo --dca-usd 50        # with weekly smart DCA
python3 -m sats_engine backtest --prices btc_1h.csv --onchain snaps.csv
python3 -m sats_engine validate --prices btc_1h.csv --onchain snaps.csv   # walk-forward verdict
python3 -m sats_engine validate --base 15m --prices btc_15m.csv            # 15m scalp / 4h swing / 1d filter
python3 -m sats_engine feed run --prices btc_1h.csv --onchain snaps.csv --validation validation.json
python3 -m sats_engine terminal feed.jsonl -f                # live dashboard (read-only, works over SSH)
python3 -m sats_engine scan                     # one block-explorer reading
python3 -m sats_engine scan --rpc --watch --save snaps.csv   # your own Knots node (pruned OK)
python3 -m sats_engine scan --watch --save snaps.csv \
    --base-url http://umbrel.local:3006/api     # or a mempool app
python3 -m unittest discover -s tests
(cd collector && npm test)                      # Worker tests, Node 22+, no installs
```

Running a node on a VPS until you have hardware: [docs/VPS_NODE.md](docs/VPS_NODE.md).
Collecting snapshots in Cloudflare with stale-data alerts: [collector/README.md](collector/README.md).
Council governance as signed mandates (Hashira 12): [docs/GOVERNANCE.md](docs/GOVERNANCE.md).
Sovereignty roadmap for the council: [SOVEREIGNTY.md](SOVEREIGNTY.md).
The unified signal feed (one hash-chained record per bar) and its consumer contract: [docs/FEED.md](docs/FEED.md).

### Data formats
- Prices: `ts,open,high,low,close[,volume]` (unix seconds, hourly bars by default)
- On-chain: `ts,fee_fast,mempool_vsize,mempool_count,whale_btc,block_fullness,hashrate`
  (any column may be blank). `scan --save` writes this format.

## Layout

```
sats_engine/
  config.py              parameters + doctrine defaults (edge floor, 72h, 1 position, -10% halt)
  models.py              Bar, OnChainSnapshot, Ticket, Fill (trace_ids)
  signals/technical.py   EMA, ATR, RSI, Kaufman efficiency ratio, rolling stats
  signals/onchain.py     On-chain Pressure Index (fees, mempool, whales, hashrate)
  strategy/swing.py      fade stretched rallies in inefficient markets
  strategy/scalp.py      fee-gated Bollinger mean reversion in chop
  strategy/accumulator.py SatsRatchet: core/sleeve, tickets, halt, smart DCA, 72h hold cap
  strategy/multitf.py    15m scalp / 4h swing / 1d trend filter on one portfolio
  timeframes.py          UTC-aligned resampling; only closed bars are emitted
  data/explorer.py       mempool.space-compatible client (self-host friendly)
  data/rpc.py            direct Knots/Core JSON-RPC backend (pruned-node compatible)
  data/loaders.py        CSV IO
  data/synthetic.py      regime-switching market for tests/demo
  backtest.py            run + score vs HODL/DCA in BTC (optional scored window)
  validate.py            walk-forward falsification test -> PASS / FAIL / INSUFFICIENT
  feed.py                the one algorithm: every layer -> one hash-chained record per bar
  terminal.py            live ANSI dashboard of the feed (no deps, NO_COLOR aware)
  data/collector.py      signed push/export client for the Cloudflare collector
  governance/            signed mandates: rules (Hashira 12), ledger replay, ssh signature checks, gov CLI
  cli.py                 demo | backtest | validate | feed | terminal | scan | collector-export | gov
collector/               Cloudflare Worker + D1 (push-only, HMAC-signed, no npm deps)
```
