# The Psyndica signal feed

One algorithm, one record per closed hourly bar, in an append-only,
hash-chained JSON Lines file. Everything internal (dashboards, agents,
alerts, council reports) reads this feed, not the engine's internals.

```
prices CSV ─┐
on-chain CSV (scan --save) ─┼─> regime + on-chain pressure + swing/scalp + DCA + sim stack ─> feed.jsonl
signed ledger (--mandates) ─┤                 + governance state + walk-forward verdict
validation.json ────────────┘
```

**The feed never instructs anyone to trade.** Signals are `would_sell` and
`would_rebuy`. Nothing in this repo places orders.

## Run it

```bash
# 1. Is there an edge at all? (repeat whenever there's materially more data)
python3 -m sats_engine validate --prices btc_1h.csv --onchain snaps.csv --out validation.json

# 2. Produce the feed (re-runs append only new bars; --follow keeps going)
python3 -m sats_engine feed run --prices btc_1h.csv --onchain snaps.csv \
    --validation validation.json --mandates governance --expect-genesis <hash> \
    --out feed.jsonl --follow

# 3. Anyone can check nothing was altered or dropped
python3 -m sats_engine feed verify feed.jsonl
```

For live use, keep appending closed hourly bars to the prices CSV and run
`scan --rpc --watch --save snaps.csv` on the node. `--follow` picks up new
rows each interval. There's no exchange price logger in the repo yet: the
prices CSV has to be maintained from an exchange export.

## Record (schema_version 1)

| Field | Meaning |
|---|---|
| `seq`, `prev_hash`, `hash`, `trace_id` | chain position; `hash` = sha256 of the canonical record without `hash`/`trace_id`; `trace_id` = first 16 hex chars of `hash` |
| `ts` | bar close, unix seconds |
| `posture` | how much weight to give the signals (below) |
| `market` | `close`, `regime` (`trending_up` / `trending_down` / `choppy` / `warming_up`), `ema`, `trend_ema`, `atr`, `rsi`, `efficiency` (Kaufman ER, 0–1) |
| `onchain` | `fresh`, `age_s`, `heat` (−1..1), `fee_z`, `mempool_z`, `whale_z`, `miner_stress` |
| `signals[]` | `would_sell`: `leg`, `target`, `stop`, `size_mult`, `taken_in_sim`, `why`. `would_rebuy`: `leg`, `exit`, `price`, `sats_delta` |
| `dca_multiplier` | 0.5–2× suggested size for the next DCA buy |
| `stack` | simulated `btc`, `hodl_btc`, `edge_vs_hodl`, `open_tickets`, `engine_halted` |
| `governance` | `mode`, `halted`, `review_required`, `ledger_head`, `genesis`, or `null` if run without a ledger |
| `validation` | `verdict`, `mean_test_edge`, `oos_trips`, `data_to`, or `null` |

`regime` describes the swing leg's view. The scalp leg warms up sooner,
so scalp signals can appear while `regime` still says `warming_up`.

## Posture, in priority order

| Posture | Meaning for consumers |
|---|---|
| `halted` | governance or the engine halted. Ignore signals. |
| `stale_data` | on-chain data missing or older than 2h. Signals are partial. |
| `validation_failed` | walk-forward says these signals lose to HODL. Informational only. |
| `unvalidated_shadow` | no walk-forward verdict. Informational only. |
| `validated_shadow` | walk-forward PASS, still shadow mode. Worth council attention. |
| `live_eligible` | PASS, the council voted `go_live`, and data is fresh. Still no executor exists, and building one needs its own vote. |

## Walk-forward verdict
- **Method:** in rolling folds, the validator picks the best of a small
  parameter grid on 90 days, then scores it on the next 30 days it has
  never seen, after 10 days of warm-up. Only those unseen results count.
- **PASS:** mean out-of-sample edge vs HODL above 0 after fees, at least
  60% of folds positive, at least 3 folds and 30 round trips.
- **INSUFFICIENT is not a pass.**
- **The synthetic year FAILS:** one fold looked like +0.8% in training and
  lost 1.1% on unseen data. That's the overfitting this exists to catch.

## Guarantees (tested)
- **Deterministic:** the same inputs produce byte-identical feeds. A
  restart resumes the chain with no duplicates.
- **Tamper-evident:** `feed verify` catches any edited, dropped, or
  reordered record.
- **No lookahead:** on-chain snapshots join a bar only if observed by its
  close.
- **Halt is respected:** under a governance halt, the simulator takes no
  new tickets.
