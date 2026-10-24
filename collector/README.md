# Sats collector (Cloudflare Worker + D1)

Stores the node scanner's on-chain snapshots in the cloud, so history
builds up even if the VPS dies, and alerts you when data stops arriving.

```
VPS: Knots (pruned) ──RPC (localhost)──> scan --rpc --push ──HTTPS, HMAC-signed──> Worker ──> D1
                                                                                     │
you: collector-export ──signed GET──────────────────────────────────────────────────┘ → snaps.csv → backtest
cron (every 5 min): stale? ──> one alert on stale, one on recovery (optional Slack webhook)
```

**Push-only by design.** Nothing connects to the node. There's no tunnel
and no open port, and the node's RPC stays on localhost.

## Security model
- Every request except `/health` is signed with HMAC-SHA256 over the
  timestamp, method, path and body. Requests more than 5 minutes off are
  rejected, which blocks replays. Signatures are compared in constant time.
- The schema is closed: unknown fields, out-of-range numbers and empty
  readings are rejected. Bodies are capped at 4 KB.
- **Keep the VPS clock synced** (`timedatectl` should show "System clock
  synchronized: yes"). More than 5 minutes of drift makes every push fail
  authentication. Rejected pushes are dropped, not retried; the local
  `--save` CSV still has them.
- Idempotent: `trace_id` is the primary key, so retries never duplicate rows.
- `/health` returns only `ok`/`stale`, with no data, so it's safe for an
  uptime monitor.
- **Cloudflare credentials never touch the VPS.** Deploy from your own
  machine. The VPS holds only the collector secret.
- **If the secret leaks,** an attacker can read the snapshots or inject fake
  ones, which would poison backtests. It cannot reach the node or any funds.
  Rotate the secret (below) and delete rows received after the leak.

## Deploy (from your own machine, not the VPS)
Requires Node 18+. Pin wrangler to an exact version that has been out for at
least two weeks, per SECURITY.md.

```bash
cd collector
npx wrangler@<version> login
npx wrangler@<version> d1 create sats-collector       # copy database_id into wrangler.toml
npx wrangler@<version> d1 execute sats-collector --remote --file=schema.sql

python3 -c "import secrets; print(secrets.token_hex(32))"   # the collector secret
npx wrangler@<version> secret put COLLECTOR_SECRET          # paste it
npx wrangler@<version> secret put ALERT_WEBHOOK             # optional: Slack incoming webhook
npx wrangler@<version> deploy                               # prints https://sats-collector.<you>.workers.dev
```

Check it: `curl -i https://sats-collector.<you>.workers.dev/health` should
return `503 stale` until the first push arrives, then `200 ok`.

## Connect the VPS scanner
See [docs/VPS_NODE.md](../docs/VPS_NODE.md) §5. The secret goes in
`/etc/sats-collector.env` (mode 0640, root:bitcoin), never on the command
line.

## Export for backtests (from your machine)
```bash
export SATS_COLLECTOR_SECRET=...   # or load it from your password manager
python3 -m sats_engine collector-export --url https://sats-collector.<you>.workers.dev \
    --source rpc --out snaps.csv
python3 -m sats_engine backtest --prices btc_1h.csv --onchain snaps.csv
```

## Rotate the secret
1. Generate a new secret, then `wrangler secret put COLLECTOR_SECRET`.
2. Update `/etc/sats-collector.env` on the VPS and run `systemctl restart sats-scan`.

During the gap, pushes are rejected and the scanner logs the error. The
local `--save` CSV on the VPS keeps the record.

## Test
```bash
cd collector && npm test     # Node 22+, uses built-in SQLite with the real schema; no installs
```
