// Sats collector: a Cloudflare Worker that stores on-chain snapshots in D1.
//
// Push-only: the scanner on the node signs each snapshot and POSTs it here.
// Nothing ever connects *to* the node, so it needs no tunnel and no open port.
//
// Every request except /health must carry:
//   X-Sats-Timestamp: unix seconds (rejected if more than 5 min off)
//   X-Sats-Signature: hex HMAC-SHA256(secret, `${ts}.${METHOD}.${path+query}.${body}`)
// The secret lives in the Worker as COLLECTOR_SECRET (wrangler secret put).
//
// Routes:
//   POST /ingest         store one snapshot (idempotent on trace_id)
//   GET  /export?source= CSV in the format sats_engine.data.loaders reads
//   GET  /health         200 "ok" / 503 "stale" - no data, safe for uptime monitors
// Cron: alerts once when snapshots go stale, once when they recover.

export const SCHEMA_VERSION = 1;
export const MAX_SKEW_S = 300;
export const MAX_BODY_BYTES = 4096;
export const SOURCES = ["rpc", "mempool-api"];
export const GENESIS_TS = 1231006505;

// Closed schema: field -> [min, max, integer?]. Unknown fields are rejected.
export const SNAPSHOT_FIELDS = {
  fee_fast: [0, 100_000, false],
  mempool_vsize: [0, 1e10, false],
  mempool_count: [0, 1e8, true],
  whale_btc: [0, 21e6, false],
  block_fullness: [0, 1.05, false],
  hashrate: [0, 1e6, false],
};
export const CSV_COLUMNS = ["ts", ...Object.keys(SNAPSHOT_FIELDS)];

const enc = new TextEncoder();

export async function sign(secret, ts, method, pathAndQuery, body) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`${ts}.${method}.${pathAndQuery}.${body}`));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function verify(request, body, secret, nowS) {
  if (!secret || secret.length < 32) return "server misconfigured";
  const ts = request.headers.get("X-Sats-Timestamp");
  const sig = request.headers.get("X-Sats-Signature") || "";
  if (!ts || !/^\d+$/.test(ts)) return "missing timestamp";
  if (Math.abs(nowS - Number(ts)) > MAX_SKEW_S) return "stale timestamp";
  const url = new URL(request.url);
  const expected = await sign(secret, ts, request.method, url.pathname + url.search, body);
  return timingSafeEqual(sig, expected) ? null : "bad signature";
}

export function validateIngest(payload, nowS) {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return "body must be an object";
  const allowed = new Set(["schema_version", "trace_id", "source", "snapshot"]);
  for (const k of Object.keys(payload)) if (!allowed.has(k)) return `unknown field: ${k}`;
  if (payload.schema_version !== SCHEMA_VERSION) return "unsupported schema_version";
  if (typeof payload.trace_id !== "string" || !/^[0-9a-f]{16,64}$/.test(payload.trace_id)) return "bad trace_id";
  if (!SOURCES.includes(payload.source)) return "bad source";

  const s = payload.snapshot;
  if (typeof s !== "object" || s === null || Array.isArray(s)) return "snapshot must be an object";
  for (const k of Object.keys(s)) if (k !== "ts" && !(k in SNAPSHOT_FIELDS)) return `unknown snapshot field: ${k}`;
  if (!Number.isInteger(s.ts) || s.ts < GENESIS_TS || s.ts > nowS + MAX_SKEW_S) return "bad snapshot.ts";
  let present = 0;
  for (const [k, [min, max, int]] of Object.entries(SNAPSHOT_FIELDS)) {
    const v = s[k];
    if (v === undefined || v === null) continue;
    if (typeof v !== "number" || !Number.isFinite(v) || v < min || v > max) return `bad ${k}`;
    if (int && !Number.isInteger(v)) return `bad ${k}`;
    present++;
  }
  // An all-empty reading means the node was syncing or down; staleness is
  // the watchdog's job, not a row in the dataset.
  if (present === 0) return "empty snapshot";
  return null;
}

function text(status, body, extra = {}) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain", ...extra } });
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}

async function readBody(request) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY_BYTES) return null;
  const body = await request.text();
  return enc.encode(body).length > MAX_BODY_BYTES ? null : body;
}

async function ingest(payload, env, nowS) {
  const s = payload.snapshot;
  const cols = Object.keys(SNAPSHOT_FIELDS);
  const res = await env.DB.prepare(
    `INSERT OR IGNORE INTO snapshots (trace_id, source, ts, ${cols.join(", ")}, received_at)
     VALUES (?, ?, ?, ${cols.map(() => "?").join(", ")}, ?)`,
  ).bind(payload.trace_id, payload.source, s.ts, ...cols.map((c) => s[c] ?? null), nowS).run();
  return json(200, { stored: res.meta.changes === 1, trace_id: payload.trace_id });
}

function csvCell(v) {
  return v === null || v === undefined ? "" : String(v);
}

async function exportCsv(url, env) {
  const source = url.searchParams.get("source") || "rpc";
  if (!SOURCES.includes(source)) return text(400, "bad source");
  const since = Number(url.searchParams.get("since") || 0);
  if (!Number.isInteger(since) || since < 0) return text(400, "bad since");
  const { results } = await env.DB.prepare(
    `SELECT ${CSV_COLUMNS.join(", ")} FROM snapshots WHERE source = ? AND ts >= ? ORDER BY ts`,
  ).bind(source, since).all();
  const lines = [CSV_COLUMNS.join(",")];
  for (const r of results) lines.push(CSV_COLUMNS.map((c) => csvCell(r[c])).join(","));
  return text(200, lines.join("\n") + "\n", { "Content-Type": "text/csv" });
}

export async function latestTs(env) {
  const row = await env.DB.prepare("SELECT MAX(ts) AS ts FROM snapshots").first();
  return row && row.ts !== null ? row.ts : null;
}

function staleSeconds(env) {
  return Number(env.STALE_SECONDS || 600);
}

export async function handleRequest(request, env, nowS = Math.floor(Date.now() / 1000)) {
  const url = new URL(request.url);

  if (request.method === "GET" && url.pathname === "/health") {
    const last = await latestTs(env);
    const fresh = last !== null && nowS - last <= staleSeconds(env);
    return text(fresh ? 200 : 503, fresh ? "ok" : "stale");
  }

  const routes = { "POST /ingest": true, "GET /export": true };
  if (!routes[`${request.method} ${url.pathname}`]) return text(404, "not found");

  const body = request.method === "POST" ? await readBody(request) : "";
  if (body === null) return text(413, "body too large");
  const authError = await verify(request, body, env.COLLECTOR_SECRET, nowS);
  if (authError) return text(authError === "server misconfigured" ? 500 : 401, authError);

  if (url.pathname === "/export") return exportCsv(url, env);

  let payload;
  try {
    payload = JSON.parse(body);
  } catch {
    return text(400, "invalid json");
  }
  const invalid = validateIngest(payload, nowS);
  if (invalid) return text(400, invalid);
  return ingest(payload, env, nowS);
}

export async function runWatchdog(env, nowS = Math.floor(Date.now() / 1000), fetchFn = fetch) {
  const last = await latestTs(env);
  const stale = last === null || nowS - last > staleSeconds(env);
  const prev = await env.DB.prepare(
    "SELECT kind FROM events WHERE kind IN ('stale', 'recovered') ORDER BY id DESC LIMIT 1",
  ).first();
  const wasStale = prev ? prev.kind === "stale" : false;
  if (stale === wasStale) return null; // alert on transitions only

  const kind = stale ? "stale" : "recovered";
  const detail = last === null ? "no snapshots yet" : `last snapshot ${nowS - last}s ago`;
  await env.DB.prepare("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)").bind(nowS, kind, detail).run();
  if (env.ALERT_WEBHOOK) {
    // Slack-compatible incoming webhook. A failed alert must not break the cron.
    try {
      await fetchFn(env.ALERT_WEBHOOK, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: `sats-collector: ${kind.toUpperCase()} - ${detail}` }),
      });
    } catch {}
  }
  return kind;
}

export default {
  fetch: (request, env) => handleRequest(request, env),
  scheduled: (_event, env, ctx) => ctx.waitUntil(runWatchdog(env)),
};
