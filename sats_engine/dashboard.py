"""`python3 -m sats_engine dashboard` - the human-in-the-loop command center.

Writes one self-contained HTML file from the feed and the signed ledger:
what needs a human decision, the shadow book in sats, data health, and the
council. It is read-only in every direction:

- It places no orders and has no way to. Signals are `would_*` only.
- It holds no keys and signs nothing. Approval happens on the operator's
  own machine with their seat key (ideally a hardware key: the
  authenticator). The page shows the exact commands; the human runs them.
- No scripts, no network: a strict Content-Security-Policy blocks both, so
  a hostile string in a mandate title or feed record can't run or phone home.
  Every value is HTML-escaped.
"""

from __future__ import annotations

import html
import os
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import feed as signal_feed
from .governance import rules
from .governance.sshsig import NAMESPACE
from .terminal import tail_records

DRAWDOWN_HALT = -0.10   # doctrine: -10% -> HALT
DRAWDOWN_WARN = -0.07
SIGNAL_LOOKBACK_S = 24 * 3600
FEED_STALE_S = 2 * 3600  # hourly bars: two missed closes
KEY_HINT = "~/.ssh/id_ed25519_sk"

LEVELS = ("critical", "action", "warn", "info")


@dataclass
class Item:
    """One thing a human should look at, most urgent first."""
    level: str
    title: str
    detail: str = ""
    commands: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ model


def drawdown(records: list[dict]) -> Optional[float]:
    """Peak-to-now change of the simulated stack (in BTC) over the records in view."""
    peak, last = None, None
    for r in records:
        btc = (r.get("stack") or {}).get("btc")
        if btc is None:
            continue
        peak = btc if peak is None else max(peak, btc)
        last = btc
    if not peak or last is None:
        return None
    return last / peak - 1


def open_mandate(state) -> Optional[object]:
    """Only the newest mandate takes votes: proposing the next one seals it."""
    return state.records[-1] if state is not None and state.records else None


def seated_for(state, rec) -> set[str]:
    """Seats that could vote on `rec`: the roster *before* it applied, as the ledger counts them."""
    seated = set(state.seated)
    if rec.applied and rec.cls == "council_change":
        for path, value in rec.mandate.get("changes", {}).items():
            s = path[len("seats."):]
            if value:
                seated.discard(s)   # seated by this very mandate: no vote on it
            else:
                seated.add(s)       # vacated by it: held the seat while it was open
    return seated


def vote_commands(ledger_dir: str, mid: str, seat: str) -> list[str]:
    d = shlex.quote(ledger_dir)
    vfile = f"{ledger_dir.rstrip('/')}/votes/{mid}/{seat}.json"
    choices = "yes|no|abstain|veto" if seat in rules.VETO_SEATS else "yes|no|abstain"
    return [
        f"python3 -m sats_engine gov vote {mid} --dir {d} --seat {seat} --vote <{choices}>",
        f"ssh-keygen -Y sign -n {NAMESPACE} -f {KEY_HINT} {shlex.quote(vfile)}",
        f"git add {shlex.quote(vfile)} {shlex.quote(vfile + '.sig')} && git commit -S -m '{seat} votes on {mid}'",
    ]


def verify_command(ledger_dir: str, genesis: Optional[str]) -> str:
    pin = f" --expect-genesis {genesis}" if genesis else " --expect-genesis <pinned-hash>"
    return f"python3 -m sats_engine gov status --dir {shlex.quote(ledger_dir)}{pin}"


def attention(records: list[dict], chain: Optional[str], state, gov_error: Optional[str],
              seat: Optional[str], ledger_dir: str, genesis_pinned: bool, now: int) -> list[Item]:
    items: list[Item] = []
    r = records[-1] if records else {}
    fgov = r.get("governance") or {}

    if chain not in (None, "ok"):
        items.append(Item("critical", "Feed chain is broken",
                          f"{chain}. Halt, preserve the file, report to Sun. Don't trust any panel below."))
    if gov_error:
        items.append(Item("critical", "Ledger did not verify",
                          f"{gov_error}. Governance state is unknown, so treat the swarm as halted."))
    halted = (state is not None and state.halted) or fgov.get("halted") \
        or (r.get("stack") or {}).get("engine_halted")
    if halted:
        items.append(Item("critical", "Swarm is HALTED",
                          "Restart is a human decision: a `restart` mandate (quorum 8, majority). "
                          "Nothing here can resume it."))

    dd = drawdown(records)
    if dd is not None and dd <= DRAWDOWN_HALT:
        items.append(Item("critical", f"Shadow drawdown {dd * 100:.1f}% (halt line -10%)",
                          "The doctrine halt level is reached in simulation. Review before anything else."))
    elif dd is not None and dd <= DRAWDOWN_WARN:
        items.append(Item("warn", f"Shadow drawdown {dd * 100:.1f}%", "Approaching the -10% halt line."))

    m = open_mandate(state)
    if m is not None and seat:
        title = f"Mandate {m.id} · {m.cls}: {m.title}"
        llm = " Drafted with an LLM: read the changes yourself before signing." \
            if m.mandate.get("drafted_with_llm") else ""
        if seat not in seated_for(state, m):
            items.append(Item("info", title, f"{seat} was not seated when this was proposed, so it has no vote here."))
        elif seat in m.votes:
            items.append(Item("info", title, f"You voted {m.votes[seat].upper()}. Status: {m.decision.reason}."))
        else:
            items.append(Item("action", f"Your vote: {title}",
                              f"Status now: {m.decision.reason}.{llm} "
                              "Sign on your own machine with your seat key; this page never signs.",
                              vote_commands(ledger_dir, m.id, seat)))
        for d in m.dropped_votes:
            if d.startswith(f"{seat}:"):
                items.append(Item("warn", f"Your vote on {m.id} was dropped", d))

    if state is not None and state.review_required:
        items.append(Item("warn", "Council review required", "A SUN override was applied. Review the next trade."))
    if state is not None and fgov.get("ledger_head") and fgov["ledger_head"] != state.head:
        items.append(Item("warn", "Feed is behind the ledger",
                          "The feed was built on an older ledger head. Re-run `feed run --mandates`."))
    if state is None and not gov_error:
        items.append(Item("warn", "No ledger attached", "Run with --mandates to see the council and your queue."))
    if state is not None and not genesis_pinned:
        items.append(Item("warn", "Genesis not pinned",
                          "Pass --expect-genesis <hash> from your password manager, or a swapped ledger loads."))
    if r.get("ts") and now - r["ts"] > FEED_STALE_S:
        items.append(Item("warn", f"Feed hasn't updated in {_ago(now - r['ts'])}",
                          "Is `feed run --follow` still running? Every panel below is old."))
    if r and r.get("posture") == "stale_data":
        items.append(Item("warn", "On-chain data is stale", "Signals are partial. Check the node and the collector."))
    if r and not r.get("validation"):
        items.append(Item("info", "No walk-forward verdict", "Signals are informational only until `validate` runs."))

    recent = [s for x in records if now - x.get("ts", 0) <= SIGNAL_LOOKBACK_S for s in x.get("signals", [])]
    if recent:
        items.append(Item("info", f"{len(recent)} shadow signal(s) in the last 24h",
                          "would_* only. They are research, never orders."))
    if not items:
        items.append(Item("info", "Nothing needs you", "No open vote, no halt, data fresh, chain intact."))
    items.sort(key=lambda i: LEVELS.index(i.level))
    return items


# ------------------------------------------------------------------- html

E = html.escape


def _num(x, fmt: str, none: str = "—") -> str:
    return none if x is None else format(x, fmt)


def _when(ts: Optional[int]) -> str:
    return "—" if not ts else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ago(s: int) -> str:
    s = max(0, int(s))
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m"


def _chart(values: list[float], w: int = 640, h: int = 120) -> str:
    """Inline SVG line of edge vs HODL with a zero line. No scripts."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return '<p class="muted">Not enough records to chart yet.</p>'
    lo, hi = min(min(vals), 0.0), max(max(vals), 0.0)
    span = (hi - lo) or 1e-9
    pad = 6

    def y(v: float) -> float:
        return pad + (hi - v) / span * (h - 2 * pad)

    step = w / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(vals))
    cls = "up" if vals[-1] >= 0 else "down"
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
            f'aria-label="Edge vs HODL, last {len(vals)} records">'
            f'<line class="zero" x1="0" x2="{w}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>'
            f'<polyline class="{cls}" points="{pts}"/></svg>'
            f'<div class="axis"><span>{vals[0] * 100:+.3f}%</span><span>edge vs HODL · dashed = 0</span>'
            f'<span>{vals[-1] * 100:+.3f}%</span></div>')


def _kv(rows: list[tuple[str, str]]) -> str:
    return "<dl>" + "".join(f"<dt>{E(k)}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


POSTURE = {
    "halted": ("crit", "HALTED"), "stale_data": ("warn", "STALE DATA"),
    "validation_failed": ("warn", "VALIDATION FAILED"), "unvalidated_shadow": ("muted", "UNVALIDATED · SHADOW"),
    "validated_shadow": ("ok", "VALIDATED · SHADOW"), "live_eligible": ("ok", "LIVE ELIGIBLE"),
}


def render_html(records: list[dict], chain: Optional[str], state, gov_error: Optional[str],
                seat: Optional[str], ledger_dir: str, genesis: Optional[str], now: int,
                refresh: Optional[int] = None) -> str:
    items = attention(records, chain, state, gov_error, seat, ledger_dir, bool(genesis), now)
    r = records[-1] if records else {}
    pcls, plabel = POSTURE.get(r.get("posture"), ("muted", str(r.get("posture") or "NO FEED")))
    mode = (state.mode if state is not None else (r.get("governance") or {}).get("mode")) or "shadow"
    st, m, oc = r.get("stack") or {}, r.get("market") or {}, r.get("onchain") or {}

    # Inbox
    inbox = []
    for it in items:
        cmds = "".join(f"<pre>{E(c)}</pre>" for c in it.commands)
        inbox.append(f'<li class="item {it.level}"><span class="tag">{it.level}</span>'
                     f"<div><strong>{E(it.title)}</strong><p>{E(it.detail)}</p>{cmds}</div></li>")

    # Halt card: any seated operator can halt alone. The safe direction is always one step away.
    halt = ""
    if seat and state is not None and seat in state.seated and not state.halted:
        d = shlex.quote(ledger_dir)
        halt = ("<section class='card halt'><h2>Emergency halt</h2>"
                "<p>Any one seated operator can halt. Proposing closes voting on the current mandate, "
                "so only do this when you mean it.</p>"
                f"<pre>{E(f'python3 -m sats_engine gov propose --dir {d} --class halt --title \"Halt: <what you saw>\" --rationale \"<evidence>\"')}</pre>"
                "<p class='muted'>Then vote yes on it and sign, with the same three commands as any vote.</p></section>")

    # Book
    edge = st.get("edge_vs_hodl")
    dd = drawdown(records)
    book = _kv([
        ("Stack (sim)", f"<span class='btc'>{_num(st.get('btc'), '.8f')} ₿</span>"),
        ("HODL", f"{_num(st.get('hodl_btc'), '.8f')} ₿"),
        ("Edge vs HODL", f"<span class='{'up' if (edge or 0) >= 0 else 'down'}'>"
                         f"{_num(edge * 100 if edge is not None else None, '+.3f')}%</span>"),
        ("Drawdown in view", f"{_num(dd * 100 if dd is not None else None, '+.2f')}% · halt at -10%"),
        ("Open tickets", E(str(st.get("open_tickets", "—"))) + " · max 1"),
        ("BTC/USD", _num(m.get("close"), ",.2f")),
        ("Regime", E(str(m.get("regime", "—")))),
        ("DCA multiplier", _num(r.get("dca_multiplier"), ".2f") + "×"),
    ])
    chart = _chart([(x.get("stack") or {}).get("edge_vs_hodl") for x in records])

    # Signals
    sig_rows = []
    for x in records:
        for s in x.get("signals", []):
            sig_rows.append((x.get("ts"), x.get("trace_id"), s))
    sig_html = "".join(
        f"<tr><td>{E(_when(ts))}</td><td>{E(str(s.get('leg', '')))}</td>"
        f"<td class='{'down' if s.get('action') == 'would_sell' else 'up'}'>{E(str(s.get('action', '')))}</td>"
        f"<td>{E(_sig_detail(s))}</td><td class='mono'>{E(str(tid or ''))}</td></tr>"
        for ts, tid, s in reversed(sig_rows[-12:])
    ) or "<tr><td colspan='5' class='muted'>No signals in view.</td></tr>"

    # Health
    val = r.get("validation") or {}
    health = _kv([
        ("Feed chain", {"ok": "<span class='up'>verified</span>", None: "not checked"}.get(
            chain, f"<span class='down'>BROKEN: {E(str(chain))}</span>")),
        ("Last bar", E(_when(r.get("ts"))) + (f" · {_ago(now - r['ts'])} ago" if r.get("ts") else "")),
        ("On-chain", ("<span class='up'>fresh</span>" if oc.get("fresh") else "<span class='warn'>STALE</span>")
         + (f" · {oc['age_s'] // 60}m old" if oc.get("age_s") is not None else "")),
        ("Heat", _num(oc.get("heat"), "+.2f")),
        ("Walk-forward", E(str(val.get("verdict", "none")))
         + (f" · mean OOS {val['mean_test_edge'] * 100:+.3f}%" if val.get("mean_test_edge") is not None else "")),
        ("Seq · trace", E(f"{r.get('seq', '—')} · {r.get('trace_id', '—')}")),
    ])

    # Council
    if state is not None:
        seats = "".join(
            f"<li class='{'on' if state.roster.get(s) else 'off'}'><b>{s}</b>"
            f"<span>{E(state.roster[s]['operator']) if state.roster.get(s) else 'vacant'}</span></li>"
            for s in rules.SEATS)
        recs = "".join(
            f"<tr><td>{E(x.id)}</td><td>{E(x.cls)}</td><td>{E(x.title)}"
            f"{' <em>[llm-drafted]</em>' if x.mandate.get('drafted_with_llm') else ''}</td>"
            f"<td class='{'up' if x.applied else 'muted'}'>{'applied' if x.applied else 'not applied'}</td>"
            f"<td>{E(x.decision.reason)}</td></tr>"
            for x in reversed(state.records[-8:])
        ) or "<tr><td colspan='5' class='muted'>No mandates yet.</td></tr>"
        council = (_kv([("Mode", E(state.mode.upper())), ("Halted", "YES" if state.halted else "no"),
                        ("Seated", f"{len(state.seated)}/12" + (" · founding phase" if state.founding else "")),
                        ("Genesis", f"<span class='mono'>{E(state.genesis_hash[:16])}…</span>"
                                    + ("" if genesis else " <span class='warn'>not pinned</span>")),
                        ("Head", f"<span class='mono'>{E(state.head[:16])}…</span>")])
                   + f"<ul class='seats'>{seats}</ul>"
                   + f"<div class='scroll'><table><thead><tr><th>#</th><th>Class</th><th>Title</th><th>State</th><th>Reason</th>"
                     f"</tr></thead><tbody>{recs}</tbody></table></div>"
                   + f"<pre>{E(verify_command(ledger_dir, genesis))}</pre>")
    else:
        council = f"<p class='muted'>{E(gov_error or 'No ledger attached. Run with --mandates governance.')}</p>"

    refresh_tag = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    who = f" · seat {E(seat)}" if seat else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<meta name="referrer" content="no-referrer">
{refresh_tag}<title>Psyndica Command Center</title>
<style>{CSS}</style></head><body>
<header><div><span class="mark">◆ PSYNDICA</span> <span>COMMAND CENTER</span></div>
<div class="meta">{E(mode.upper())}{who} · generated {E(_when(now))}</div></header>
<div class="banner {pcls}"><b>{E(plabel)}</b><span>Read-only. No orders, no keys, no signing. Signals are would_* only.</span></div>
<main>
<section class="card inbox"><h2>Needs you</h2><ul>{''.join(inbox)}</ul></section>
{halt}
<section class="card"><h2>Shadow book · measured in BTC</h2>{book}{chart}</section>
<section class="card"><h2>Data health</h2>{health}</section>
<section class="card wide"><h2>Signals · shadow only, never orders</h2>
<div class='scroll'><table><thead><tr><th>Bar</th><th>Leg</th><th>Signal</th><th>Detail</th><th>trace_id</th></tr></thead>
<tbody>{sig_html}</tbody></table></div></section>
<section class="card wide"><h2>Council · Hashira 12</h2>{council}</section>
</main>
<footer>Approve with your seat key on your own device. Verify with <code>gov status</code>. When in doubt, halt and ask Sun.</footer>
</body></html>
"""


def _sig_detail(s: dict) -> str:
    if s.get("action") == "would_sell":
        d = f"target {_num(s.get('target'), ',.0f')} · stop {_num(s.get('stop'), ',.0f')}"
        if s.get("blocked_by_htf"):
            d += " · blocked by 1d trend"
        elif not s.get("taken_in_sim"):
            d += " · not taken (caps)"
        return d
    return f"{s.get('exit', '')} · {s.get('sats_delta', 0):+,} sats"


CSS = """
:root{--bg:#0d0f12;--card:#161a20;--ink:#e6e8eb;--muted:#8a929c;--line:#252b33;
--gold:#d4a017;--cyan:#38bdf8;--violet:#a78bfa;--up:#34d399;--down:#f87171;--warn:#fbbf24;--crit:#ef4444}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--muted:#5d6670;
--line:#e3e6ea;--gold:#9a6f00;--cyan:#0369a1;--violet:#6d28d9;--up:#047857;--down:#b91c1c;--warn:#a16207;--crit:#b91c1c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:14px 16px;border-bottom:1px solid var(--line)}
.mark{color:var(--gold);font-weight:700;letter-spacing:.06em}.meta{color:var(--muted)}
.banner{display:flex;gap:12px;flex-wrap:wrap;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line)}
.banner span{color:var(--muted)}.banner.crit b{color:var(--crit)}.banner.warn b{color:var(--warn)}
.banner.ok b{color:var(--up)}.banner.muted b{color:var(--muted)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:16px;align-items:start;padding:16px;max-width:1280px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.wide,.inbox{grid-column:1/-1}
h2{margin:0 0 12px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
ul{list-style:none;margin:0;padding:0}
.item{display:flex;gap:12px;padding:10px 0;border-top:1px solid var(--line)}.item:first-child{border-top:0}
.item>div{flex:1;min-width:0}.item p{margin:4px 0;color:var(--muted)}
.tag{flex:0 0 auto;font-size:11px;text-transform:uppercase;font-weight:700;padding:2px 8px;border-radius:99px;
height:fit-content;border:1px solid currentColor}
.critical .tag{color:var(--crit)}.action .tag{color:var(--cyan)}.warn .tag{color:var(--warn)}.info .tag{color:var(--muted)}
pre{margin:6px 0;padding:8px 10px;background:var(--bg);border:1px solid var(--line);border-radius:6px;
overflow-x:auto;white-space:pre;font:12px/1.4 ui-monospace,Menlo,monospace;user-select:all}
.halt{border-color:var(--crit)}.halt h2{color:var(--crit)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0 0 12px}dt{color:var(--muted)}dd{margin:0;min-width:0;overflow-wrap:anywhere}
.btc{color:var(--gold);font-weight:600}.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
svg{width:100%;height:120px;display:block}svg .zero{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
svg polyline{fill:none;stroke-width:1.5;vector-effect:non-scaling-stroke}
svg polyline.up{stroke:var(--up)}svg polyline.down{stroke:var(--down)}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;border-top:0}
.seats{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px;margin:0 0 12px}
.seats li{padding:6px 8px;border:1px solid var(--line);border-radius:6px;display:flex;flex-direction:column}
.seats li.on b{color:var(--cyan)}.seats li.off{opacity:.55}.seats span{color:var(--muted);font-size:12px}
em{color:var(--violet);font-style:normal}code{font-family:ui-monospace,Menlo,monospace}
footer{padding:16px;color:var(--muted);text-align:center;border-top:1px solid var(--line)}
"""


# -------------------------------------------------------------------- run


def write_atomic(path: str | Path, text: str) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".dashboard-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def build(feed_path: str, ledger_dir: Optional[str], genesis: Optional[str], seat: Optional[str],
          verify: bool, history: int, load_ledger, now: Optional[int] = None,
          refresh: Optional[int] = None) -> str:
    """Gather inputs and render. `load_ledger(dir, expect_genesis)` returns a ledger State
    or raises; any failure is shown on the page, never swallowed."""
    chain = None
    if verify and Path(feed_path).exists():
        try:
            signal_feed.verify(feed_path)
            chain = "ok"
        except signal_feed.FeedError as e:
            chain = str(e)
    state, gov_error = None, None
    if ledger_dir:
        try:
            state = load_ledger(ledger_dir, genesis)
        except Exception as e:  # LedgerError, SignatureError, missing ssh-keygen: fail closed, loudly
            gov_error = f"{type(e).__name__}: {e}"
    return render_html(tail_records(feed_path, history), chain, state, gov_error, seat,
                       ledger_dir or "governance", genesis, int(now if now is not None else time.time()), refresh)


def run(feed_path: str, out: str, ledger_dir: Optional[str], genesis: Optional[str], seat: Optional[str],
        follow: bool, interval: float, verify: bool, history: int, load_ledger) -> int:
    refresh = max(5, int(interval)) if follow else None
    try:
        while True:
            write_atomic(out, build(feed_path, ledger_dir, genesis, seat, verify, history, load_ledger,
                                    refresh=refresh))
            if not follow:
                print(f"wrote {out}  (open it in a browser; read-only, no scripts)")
                return 0
            print(f"updated {out} at {_when(int(time.time()))}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
