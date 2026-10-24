// Run: node --test collector/test/   (Node 22+, no npm install needed)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { handleRequest, runWatchdog, sign, validateIngest } from "../src/index.js";

const SECRET = "k".repeat(32);
const NOW = 1792840000;
const BASE = "https://collector.example";

// Minimal D1 shim over real SQLite with the real schema.
function makeD1() {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  return {
    prepare(sql) {
      let args = [];
      const stmt = {
        bind: (...a) => ((args = a), stmt),
        run: async () => ({ meta: { changes: db.prepare(sql).run(...args).changes } }),
        first: async () => db.prepare(sql).get(...args) ?? null,
        all: async () => ({ results: db.prepare(sql).all(...args) }),
      };
      return stmt;
    },
  };
}

const env = (extra = {}) => ({ DB: makeD1(), COLLECTOR_SECRET: SECRET, STALE_SECONDS: "600", ...extra });

const payload = (over = {}) => ({
  schema_version: 1,
  trace_id: "abcdef0123456789",
  source: "rpc",
  snapshot: { ts: NOW - 60, fee_fast: 12.5, mempool_vsize: 3e7, mempool_count: 25000, hashrate: 800 },
  ...over,
});

async function signedRequest(method, pathAndQuery, body = "", { ts = NOW, secret = SECRET } = {}) {
  return new Request(BASE + pathAndQuery, {
    method,
    body: method === "POST" ? body : undefined,
    headers: {
      "X-Sats-Timestamp": String(ts),
      "X-Sats-Signature": await sign(secret, ts, method, pathAndQuery, body),
    },
  });
}

const ingest = async (e, p, opts) =>
  handleRequest(await signedRequest("POST", "/ingest", JSON.stringify(p), opts), e, NOW);

test("signature matches the Python client (cross-language vector)", async () => {
  assert.equal(
    await sign(SECRET, 1792840000, "POST", "/ingest", '{"a":1}'),
    "12d8bb16c6f9306355a30d8c89f722a15f56af2475f7a6c644538179d9e6b64d",
  );
});

test("ingest stores once, retries are idempotent", async () => {
  const e = env();
  const r1 = await ingest(e, payload());
  assert.equal(r1.status, 200);
  assert.deepEqual(await r1.json(), { stored: true, trace_id: "abcdef0123456789" });
  const r2 = await ingest(e, payload());
  assert.deepEqual(await r2.json(), { stored: false, trace_id: "abcdef0123456789" });
});

test("rejects bad signature, wrong secret, and replayed timestamps", async () => {
  const e = env();
  assert.equal((await ingest(e, payload(), { secret: "x".repeat(32) })).status, 401);
  assert.equal((await ingest(e, payload(), { ts: NOW - 301 })).status, 401);
  const tampered = await signedRequest("POST", "/ingest", JSON.stringify(payload()));
  const forged = new Request(tampered, { body: JSON.stringify(payload({ trace_id: "f".repeat(16) })) });
  assert.equal((await handleRequest(forged, e, NOW)).status, 401);
  const unsigned = new Request(BASE + "/ingest", { method: "POST", body: "{}" });
  assert.equal((await handleRequest(unsigned, e, NOW)).status, 401);
});

test("refuses to run with a weak or missing secret", async () => {
  const e = env({ COLLECTOR_SECRET: "short" });
  assert.equal((await ingest(e, payload())).status, 500);
});

test("closed schema: unknown fields and out-of-range values are rejected", () => {
  assert.match(validateIngest(payload({ extra: 1 }), NOW), /unknown field/);
  assert.match(validateIngest(payload({ snapshot: { ts: NOW, evil: 1 } }), NOW), /unknown snapshot field/);
  assert.match(validateIngest(payload({ snapshot: { ts: NOW, block_fullness: 7 } }), NOW), /bad block_fullness/);
  assert.match(validateIngest(payload({ snapshot: { ts: NOW, mempool_count: 1.5 } }), NOW), /bad mempool_count/);
  assert.match(validateIngest(payload({ snapshot: { ts: NOW + 999, fee_fast: 1 } }), NOW), /bad snapshot.ts/);
  assert.match(validateIngest(payload({ snapshot: { ts: NOW } }), NOW), /empty snapshot/);
  assert.match(validateIngest(payload({ source: "other" }), NOW), /bad source/);
  assert.match(validateIngest(payload({ schema_version: 2 }), NOW), /schema_version/);
  assert.equal(validateIngest(payload(), NOW), null);
});

test("oversized bodies are refused before parsing", async () => {
  const e = env();
  const big = JSON.stringify(payload({ trace_id: "a".repeat(5000) }));
  assert.equal((await handleRequest(await signedRequest("POST", "/ingest", big), e, NOW)).status, 413);
});

test("export returns loader-format CSV for one source only", async () => {
  const e = env();
  await ingest(e, payload());
  await ingest(e, payload({ trace_id: "1".repeat(16), snapshot: { ts: NOW - 30, whale_btc: 150.5 } }));
  await ingest(e, payload({ trace_id: "2".repeat(16), source: "mempool-api", snapshot: { ts: NOW, fee_fast: 9 } }));
  const r = await handleRequest(await signedRequest("GET", "/export?source=rpc&since=0"), e, NOW);
  assert.equal(r.status, 200);
  assert.equal(
    await r.text(),
    "ts,fee_fast,mempool_vsize,mempool_count,whale_btc,block_fullness,hashrate\n" +
      `${NOW - 60},12.5,30000000,25000,,,800\n` +
      `${NOW - 30},,,,150.5,,\n`,
  );
  const unsigned = new Request(BASE + "/export?source=rpc&since=0");
  assert.equal((await handleRequest(unsigned, e, NOW)).status, 401);
});

test("health reveals freshness only", async () => {
  const e = env();
  const h = () => handleRequest(new Request(BASE + "/health"), e, NOW);
  assert.equal((await h()).status, 503);
  await ingest(e, payload());
  const r = await h();
  assert.equal(r.status, 200);
  assert.equal(await r.text(), "ok");
});

test("unknown routes 404", async () => {
  assert.equal((await handleRequest(new Request(BASE + "/admin"), env(), NOW)).status, 404);
});

test("watchdog alerts on transitions only", async () => {
  const e = env({ ALERT_WEBHOOK: "https://hooks.example/x" });
  const sent = [];
  const fetchFn = async (url, init) => sent.push(JSON.parse(init.body).text);

  assert.equal(await runWatchdog(e, NOW, fetchFn), "stale"); // nothing yet
  assert.equal(await runWatchdog(e, NOW + 300, fetchFn), null); // still stale, no spam
  await ingest(e, payload({ snapshot: { ts: NOW, fee_fast: 5 } }));
  assert.equal(await runWatchdog(e, NOW + 60, fetchFn), "recovered");
  assert.equal(await runWatchdog(e, NOW + 120, fetchFn), null);
  assert.equal(await runWatchdog(e, NOW + 700, fetchFn), "stale");
  assert.equal(sent.length, 3);
  assert.match(sent[0], /STALE - no snapshots yet/);
});

test("a failing alert webhook does not break the watchdog", async () => {
  const e = env({ ALERT_WEBHOOK: "https://hooks.example/x" });
  const boom = async () => { throw new Error("down"); };
  assert.equal(await runWatchdog(e, NOW, boom), "stale");
});
