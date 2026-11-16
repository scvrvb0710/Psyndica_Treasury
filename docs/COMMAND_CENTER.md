# Command center

One HTML page that puts the operator in the loop: what needs a human
decision, the shadow book in BTC, data health, and the council.

```bash
python3 -m sats_engine dashboard feed.jsonl \
    --mandates governance --expect-genesis <pinned-hash> \
    --seat WATER --out dashboard.html --follow      # rewrites every 30s
```

Open `dashboard.html` in a browser. It reloads itself while `--follow` runs.

## The loop

1. **Needs you** lists items by urgency: critical, then action, warn, info.
   Examples: a broken feed chain, a ledger that doesn't verify, a halt,
   shadow drawdown near -10%, the open mandate waiting on *your* seat, a
   stale feed, an unpinned genesis.
2. For a vote, the page prints three commands: write the vote, sign it,
   commit it. Run them **on your own machine**. The signature comes from
   your seat key. A hardware key (`ed25519-sk`, e.g. a YubiKey) is the
   authenticator: signing needs a physical touch.
3. Re-render (or wait for `--follow`). `gov status` verifies the signature
   and the page shows your vote and the new state.
4. **Emergency halt** is always one card away for a seated operator. Note
   that proposing a halt closes voting on the current mandate.

The page has no restart button. Restart is a council `restart` mandate.

## What it will never do
- Place, stage, or suggest orders. Signals are `would_*` research only.
- Hold, request, or use keys, or sign anything.
- Run scripts or load anything from the network. A strict CSP
  (`default-src 'none'`) enforces this. Every value is HTML-escaped, so a
  hostile mandate title or feed record renders as text.
- Hide a failure. If the ledger doesn't verify (bad signature, wrong
  genesis, no `ssh-keygen`), the page says so in red and treats the swarm
  as halted.

## Limits (Moon's objection)
- **It's a snapshot, not a source of truth.** Anyone who can write the
  HTML file can lie on it. Before signing, run the `gov status` command
  printed on the page and read the mandate file itself.
- **Drawdown is "in view"**: peak-to-now over the last `--history`
  records, not all-time.
- **Hosting it** (e.g. Cloudflare Pages behind Cloudflare Access with
  hardware-key MFA) is a separate decision. The page holds no secrets, but
  it shows treasury posture. Keep it private.
