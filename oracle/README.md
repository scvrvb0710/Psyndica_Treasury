# Psyndica oracle

A defensive cyber-security and OPSEC advisor. It runs on Cloudflare Workers AI
with open-weight models only, so there is no third-party API key.

**Tier 3, Infrastructure.** It holds no funds and no keys, and it stores
nothing. It answers questions and never takes actions. Its answers are
advisory: check them before you act. If you suspect a compromise, halt first
and report to Sun.

## How a question is handled

```
POST /ask ─► bearer token ─► rate limit (20/min) ─► secret scan ─► guard(question)
                                                       │ hit: 422, nothing sent
          ◄── JSON + advisory ◄── guard(answer) ◄── primary model ─► fallback on error
```

| Stage | Default | Why |
|---|---|---|
| Secret scan | local regex | Questions containing PEM keys, xprv, WIF, 64-hex, cloud/API tokens, JWTs or seed-like word runs are **rejected before leaving the Worker**. The reply tells the caller to rotate or sweep. |
| Guard | `@cf/meta/llama-guard-3-8b` | Screens the question and the answer. Unparseable verdicts count as unsafe (fails closed). |
| Primary | `@cf/openai/gpt-oss-120b` | Open-weight reasoning model. |
| Fallback | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | Used when the primary errors or returns nothing. |

To swap models, edit `[vars]` in `wrangler.toml`. No code changes needed.

## API

```bash
curl -s https://psyndica-oracle.<sub>.workers.dev/ask \
  -H "Authorization: Bearer $ORACLE_TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"threat","question":"Threat-model our 2-of-3 multisig: who holds what, what can go wrong?"}'
```

Request: `question` (required, ≤4000 chars), `context` (optional, ≤10000
chars, such as logs, headers, configs or a suspicious email; the oracle
treats it as untrusted data, never as instructions), `mode`:

- `opsec`: identity separation, metadata, device and account hygiene. This is the default.
- `threat`: assets, adversaries, attack paths, gaps, prioritised mitigations.
- `review`: findings ranked by severity for a config, policy or code you paste.
- `incident`: contain, preserve, check blast radius by tier, eradicate, recover, report.

Response: `{schema_version, trace_id, mode, model, blocked, categories, answer, advisory}`.
`blocked` is `null`, `"input"` or `"output"`.

## What it will not do

It is defensive only. It explains attacks at the depth a defender needs, but
it will not write working malware, phishing kits, or intrusion steps against
systems you do not own. The guard model can also refuse legitimate defensive
questions. If that happens, rephrase the question around the system you are
protecting.

## Privacy and logging

Logs contain `trace_id`, mode, model, guard verdicts, character counts and
latency. They **never** contain question, context or answer text, because
logging what you ask a security oracle would itself be an OPSEC leak. The
current Workers AI catalogue lists `llama-guard-3-8b` as zero-data-retention.
Check the retention terms for whichever primary and fallback models you
choose before you send sensitive context.

## Run and deploy

```bash
cd oracle && npm test                       # Node 22+, no install
WRANGLER_VERSION=4.x.y ./deploy.sh          # from your own machine
```

The Worker fails closed (HTTP 500) until `ORACLE_TOKEN` is set. For more than
one user, consider putting Cloudflare Access in front of the workers.dev
route as well.
