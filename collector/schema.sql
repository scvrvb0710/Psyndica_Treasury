-- D1 schema for the sats collector.
-- Apply: npx wrangler d1 execute sats-collector --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS snapshots (
  trace_id       TEXT PRIMARY KEY,           -- idempotency key: retries never duplicate
  source         TEXT NOT NULL CHECK (source IN ('rpc', 'mempool-api')),
  ts             INTEGER NOT NULL,
  fee_fast       REAL,
  mempool_vsize  REAL,
  mempool_count  INTEGER,
  whale_btc      REAL,
  block_fullness REAL,
  hashrate       REAL,
  received_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_source_ts ON snapshots (source, ts);

CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  kind   TEXT NOT NULL,                      -- 'stale' | 'recovered'
  detail TEXT
);
