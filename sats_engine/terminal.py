"""`python3 -m sats_engine terminal feed.jsonl` - a live, read-only view of the feed.

Plain ANSI, no dependencies, works over SSH. It reads the feed file and
nothing else: no keys, no wallets, no network. Colours follow the stack -
gold for BTC, cyan for ADA/governance, violet for NIGHT/privacy - and are
switched off for non-terminals or when NO_COLOR is set.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import feed as signal_feed

SPARK = "▁▂▃▄▅▆▇█"

POSTURE_STYLE = {
    "halted": ("red", "■ HALTED"),
    "stale_data": ("yellow", "◌ STALE DATA"),
    "validation_failed": ("magenta", "✕ VALIDATION FAILED"),
    "unvalidated_shadow": ("dim", "○ UNVALIDATED · SHADOW"),
    "validated_shadow": ("cyan", "● VALIDATED · SHADOW"),
    "live_eligible": ("green", "● LIVE ELIGIBLE"),
}

CODES = {
    "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "magenta": "35",
    "cyan": "36", "gold": "38;5;178", "violet": "38;5;141", "grey": "38;5;244",
}


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "\x1b[" + ";".join(CODES[s] for s in styles) + "m" + text + "\x1b[0m"


def tail_records(path: str | Path, n: int) -> list[dict]:
    """Last `n` records without reading the whole file."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size, block, data = f.tell(), 1 << 16, b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    lines = [ln for ln in data.splitlines() if ln.strip()][-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # a partial line mid-write; it'll be whole next refresh
    return out


def sparkline(values: list[float], width: int) -> str:
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return SPARK[3] * len(values)
    return "".join(SPARK[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in values)


def _num(x, fmt: str, none: str = "—") -> str:
    return none if x is None else format(x, fmt)


def _ts(ts: Optional[int]) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


def _regime(p: Painter, regime: Optional[str]) -> str:
    style = {"trending_up": "green", "trending_down": "red", "choppy": "yellow"}.get(regime or "", "dim")
    return p((regime or "—").replace("_", " "), style)


def _heat_bar(p: Painter, heat: Optional[float], cells: int = 10) -> str:
    if heat is None:
        return p("·" * cells, "dim")
    filled = round((heat + 1) / 2 * cells)
    style = "red" if heat > 0.5 else "yellow" if heat > 0 else "cyan"
    return p("▮" * filled, style) + p("▯" * (cells - filled), "dim")


def render(records: list[dict], width: int = 80, color: bool = True, chain: Optional[str] = None) -> str:
    """One full frame as a string. Pure: easy to test, easy to pipe."""
    p = Painter(color)
    width = max(60, min(width, 120))
    rule = p("─" * width, "grey")

    def section(title: str) -> str:
        return p("─ ", "grey") + p(title, "bold") + " " + p("─" * (width - len(title) - 3), "grey")

    head = p(" ◆ PSYNDICA", "gold", "bold") + p(" · SATS RATCHET", "bold")
    if not records:
        return "\n".join([head, rule, p("  waiting for the first feed record…", "dim"),
                          p("  run: python3 -m sats_engine feed run --prices <csv> --out feed.jsonl", "grey")])

    r = records[-1]
    gov = r.get("governance") or {}
    mode = (gov.get("mode") or "shadow").upper()
    right = f"{mode} · {r.get('base_timeframe', '1h')} · {_ts(r.get('ts'))}"
    lines = [head + " " * max(1, width - 26 - len(right)) + p(right, "grey"), rule]

    style, label = POSTURE_STYLE.get(r.get("posture"), ("dim", str(r.get("posture"))))
    m, st = r.get("market", {}), r.get("stack", {})
    edge = st.get("edge_vs_hodl")
    edge_s = p(_num(edge * 100 if edge is not None else None, "+.3f") + "%",
               "green" if (edge or 0) > 0 else "red" if (edge or 0) < 0 else "dim")
    lines.append(f"  {p('POSTURE', 'grey')}  {p(label, style, 'bold')}")
    lines.append(f"  {p('BTC/USD', 'grey')}  {p(_num(m.get('close'), ',.2f'), 'gold', 'bold')}"
                 f"    {p('stack', 'grey')} {p(_num(st.get('btc'), '.8f') + ' ₿', 'gold')}"
                 f"    {p('vs HODL', 'grey')} {edge_s}")
    edges = [x.get("stack", {}).get("edge_vs_hodl") for x in records]
    edges = [e for e in edges if e is not None]
    lines.append(f"  {p('edge', 'grey')}     {p(sparkline(edges, width - 13), 'gold')}")

    lines.append(section("TIMEFRAMES"))
    tfs = r.get("timeframes")
    if tfs:
        t15, t4, t1 = tfs.get("15m", {}), tfs.get("4h", {}), tfs.get("1d", {})
        lines.append(f"  {p('15m', 'bold')}  close {_num(t15.get('close'), ',.2f'):>12}"
                     f"   bb z {_num(t15.get('bb_z'), '+.2f'):>6}   eff {_num(t15.get('efficiency'), '.2f')}")
        lines.append(f"  {p(' 4h', 'bold')}  {_regime(p, t4.get('regime')):<24}"
                     f" rsi {_num(t4.get('rsi'), '.0f'):>4}   eff {_num(t4.get('efficiency'), '.2f')}"
                     f"   atr {_num(t4.get('atr'), ',.0f')}")
        lines.append(f"  {p(' 1d', 'bold')}  {_regime(p, t1.get('regime')):<24}"
                     f" ema {_num(t1.get('ema'), ',.0f'):>8}   eff {_num(t1.get('efficiency'), '.2f')}")
    else:
        lines.append(f"  {p(r.get('base_timeframe', '1h'), 'bold')}  {_regime(p, m.get('regime'))}"
                     f"   rsi {_num(m.get('rsi'), '.0f')}   eff {_num(m.get('efficiency'), '.2f')}"
                     f"   atr {_num(m.get('atr'), ',.0f')}")

    oc = r.get("onchain", {})
    fresh = p("fresh", "green") if oc.get("fresh") else p("STALE", "yellow", "bold")
    age = oc.get("age_s")
    lines.append(section("ON-CHAIN"))
    lines.append(f"  heat {_heat_bar(p, oc.get('heat'))} {_num(oc.get('heat'), '+.2f')}"
                 f"   fee z {_num(oc.get('fee_z'), '+.1f')}   mempool z {_num(oc.get('mempool_z'), '+.1f')}"
                 f"   whale z {_num(oc.get('whale_z'), '+.1f')}")
    lines.append(f"  {fresh}{p(f' · {age // 60}m old' if age is not None else '', 'grey')}"
                 f"{p('   ⚠ miner stress', 'yellow') if oc.get('miner_stress') else ''}"
                 f"   {p('dca ×', 'grey')}{_num(r.get('dca_multiplier'), '.2f')}")

    lines.append(section("SIGNALS · shadow only, never orders"))
    recent = [(x["ts"], s) for x in records for s in x.get("signals", [])][-6:]
    if not recent:
        lines.append(p("  no signals in view", "dim"))
    for ts, s in reversed(recent):
        if s["action"] == "would_sell":
            tag = p("WOULD SELL ", "red", "bold")
            detail = f"tgt {_num(s.get('target'), ',.0f')}  stop {_num(s.get('stop'), ',.0f')}"
            if s.get("blocked_by_htf"):
                detail += p("  blocked by 1d trend", "yellow")
            elif not s.get("taken_in_sim"):
                detail += p("  not taken (caps)", "grey")
        else:
            tag = p("WOULD REBUY", "green", "bold")
            sats = s.get("sats_delta", 0)
            detail = f"{s.get('exit', ''):<10} " + p(f"{sats:+,} sats", "green" if sats > 0 else "red")
        lines.append(f"  {p(_hhmm(ts), 'grey')}  {s['leg']:<5}  {tag}  {detail}")

    lines.append(section("GOVERNANCE · VALIDATION"))
    if gov:
        halted = p("HALTED", "red", "bold") if gov.get("halted") else p("running", "green")
        review = p("  review required", "yellow") if gov.get("review_required") else ""
        lines.append(f"  {p('council', 'cyan')}  mode {gov.get('mode')}  ·  {halted}"
                     f"  ·  ledger {str(gov.get('ledger_head'))[:12]}…{review}")
    else:
        lines.append(f"  {p('council', 'cyan')}  {p('no ledger attached (run with --mandates)', 'dim')}")
    val = r.get("validation")
    if val:
        vstyle = {"PASS": "green", "FAIL": "red"}.get(val.get("verdict"), "yellow")
        me = val.get("mean_test_edge")
        lines.append(f"  {p('walk-fwd', 'violet')} {p(str(val.get('verdict')), vstyle, 'bold')}"
                     f"  mean OOS {_num(me * 100 if me is not None else None, '+.3f')}%"
                     f"  ·  {val.get('oos_trips')} trips  ·  data to {_ts(val.get('data_to'))}")
    else:
        lines.append(f"  {p('walk-fwd', 'violet')} {p('no verdict (run validate)', 'dim')}")

    lines.append(rule)
    chain_s = {None: p("chain not checked", "grey"), "ok": p("chain ✓", "green")}.get(
        chain, p(f"chain BROKEN: {chain}", "red", "bold"))
    lines.append(f"  {p('seq', 'grey')} {r.get('seq')}  {p('·', 'grey')}  {p('trace', 'grey')} {r.get('trace_id')}"
                 f"  {p('·', 'grey')}  {chain_s}")
    return "\n".join(lines)


def run(path: str, follow: bool, interval: float, verify: bool, history: int = 500) -> int:
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    chain = None
    if verify and Path(path).exists():
        try:
            signal_feed.verify(path)
            chain = "ok"
        except signal_feed.FeedError as e:
            chain = str(e)
    try:
        while True:
            width = shutil.get_terminal_size((100, 40)).columns
            frame = render(tail_records(path, history), width, color, chain)
            if follow and color:
                sys.stdout.write("\x1b[H\x1b[2J")
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            if not follow:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
