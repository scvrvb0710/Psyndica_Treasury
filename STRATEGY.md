# Sats Ratchet: strategy design

**Status: research / shadow mode. Not approved for live trading.**
Under Hashira Beta v0.1.2 the only live strategy is SPOT_PERP_CARRY. This
engine trades directionally, so going live takes a 9-of-12 council vote,
then shadow mode, then Sun's go-live vote.

## Thesis

BTC stores. The goal is **more BTC**, not more dollars. So the engine keeps
its books in BTC and judges every trade by one test: did the round trip end
with more sats than it started with?

Three ideas combine:

1. **The sats round trip (the ratchet).** Sell part of the stack into an
   over-extended rally, ring-fence the USD, and buy back lower. The USD is
   never free money: every ticket has a target (passive limit, more sats), a
   stop (price ran away, buy back and take a bounded sats loss), and a 72h
   expiry. The engine is never left holding open-ended fiat while BTC leaves
   without it.
2. **Market efficiency as the regime filter.** The Kaufman efficiency ratio
   (ER) measures how straight price has travelled. High ER plus price above
   the 200 EMA means an efficient uptrend, and fading that is how stacks
   shrink, so the swing leg stands down. The scalp leg only trades when ER is
   low, meaning the market is churning and inefficient enough to mean-revert.
3. **Block-explorer pressure as confirmation, never as trigger.** The
   On-chain Pressure Index (OPI) z-scores next-block fees, mempool backlog
   and whale-sized mempool transfers. Urgent on-chain activity tends to show
   up at distribution tops and panic bottoms. Price structure decides the
   direction; heat only scales the size. Miner stress (hashrate falling off
   its peak) plus price stretched below trend raises DCA size.

## Legs

| Leg | Fires when | Target | Stop | Hold |
|---|---|---|---|---|
| Swing | close ≥ 2 ATR over EMA50, RSI ≥ 68, *not* an efficient uptrend | −2.5 ATR (≥ edge floor) | +2.5 ATR | 72h |
| Scalp | ER ≤ 0.30, close above the 2.2σ Bollinger band, distance to mean ≥ max(2.5× fees, edge floor) | band mean | +1.5σ | 12h |
| Smart DCA (opt.) | weekly | — | — | — |

Smart DCA spends 0.5–2× the contribution. It buys more when price is
stretched below trend, more again on on-chain panic or miner stress. Unspent
cash is held, not churned.

## Doctrine controls (enforced in code, tested)

- Edge floor: target distance ≥ max(2 × round-trip cost, 0.15%).
- Max 1 open position across legs (`max_open_total`).
- 72h hold cap on swing tickets.
- −10% stack drawdown (in BTC) → sticky HALT. Only `resume()` clears it.
- Sleeve cap: at most 30% of the stack is ever out on tickets. The 70% core
  is never sold.
- Pessimistic fills: if one bar touches both target and stop, the stop wins.
  Gaps fill at the open.
- Limit-only order model: passive limits pay maker, marketable limits pay
  taker plus slippage.
- `trace_id` on every ticket and fill. Skipped signals and halts go to
  `engine.decisions`.

## Moon's objection (red team)

The case against, which has to be answered before any vote:

- **Fading BTC loses in bull markets.** The synthetic demo trends about 3×
  and the engine ends about 5% *behind* HODL in BTC terms. Stops fire more
  often than targets: 113 stops vs. 90 targets, 42–46% win rate. Each stop is
  a guaranteed sats loss, and in a secular uptrend, "sell high, buy back
  lower" often becomes "sell, buy back higher."
- **Scalping is a fee donation.** At retail fee tiers (0.1% to 0.2%), the
  edge floor blocks most scalps. The ones that pass are rare, small, and
  still net-negative on synthetic data.
- **On-chain heat is noisy and mostly unproven.** Mempool fee spikes follow
  ordinals, inscriptions and consolidations as often as they follow
  distribution. `/mempool/recent` sees only a small window of transactions,
  so whale flow without exchange address labels is a weak proxy.
- **Synthetic results prove nothing.** They show the plumbing works. They
  carry no information about edge.
- **Tax drag.** Every sell is a taxable event. Gains measured in BTC can
  still be losses after tax in USD.

**Falsification test (shadow mode):** run on at least 2 years of real hourly
bars plus logged on-chain snapshots, walk-forward, at the venue's real fees.
If edge vs. HODL after fees is ≤ 0 or below the edge floor, or fewer than
30 round trips qualify, the thesis is unproven. Then park it and stay with
carry plus DCA. Do not trade it anyway.

## Path to a vote (if the shadow results justify one)

1. Pull real data: exchange OHLCV CSVs, plus `scan --watch --save` running
   on your own mempool node for weeks to build an on-chain history.
2. Walk-forward parameter search. No tuning on the full sample.
3. Shadow mode alongside the live carry swarm: log decisions, zero orders.
4. Present results plus Moon's case to the council (9 of 12 for a new
   strategy).
5. Only then: an executor built to the v0.1.2 executor spec, inside the
   designated swarm wallet, under the $500 cap.
