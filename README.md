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
python3 -m sats_engine scan                     # one block-explorer reading
python3 -m sats_engine scan --watch --save snaps.csv \
    --base-url http://umbrel.local:3006/api     # build on-chain history from your own node
python3 -m unittest discover -s tests
```

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
  strategy/accumulator.py SatsRatchet: core/sleeve, tickets, halt, smart DCA
  data/explorer.py       mempool.space-compatible client (self-host friendly)
  data/loaders.py        CSV IO
  data/synthetic.py      regime-switching market for tests/demo
  backtest.py            run + score vs HODL/DCA in BTC
  cli.py                 demo | backtest | scan
```
