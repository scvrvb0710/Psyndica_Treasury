"""`python3 -m sats_engine gov ...` - author, vote on, and inspect mandates.

The CLI only writes unsigned files. Signing happens on each operator's own
machine with their own key (ideally a hardware key), using ssh-keygen:

    ssh-keygen -Y sign -n psyndica-mandate -f ~/.ssh/id_ed25519_sk <file>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ledger, rules
from .sshsig import NAMESPACE, SignatureError, SSHVerifier, normalize_key

SIGN_HINT = "ssh-keygen -Y sign -n " + NAMESPACE + " -f <your-private-key> {path}"


def _read_key(path: str) -> str:
    try:
        return normalize_key(Path(path).expanduser().read_text().splitlines()[0])
    except (OSError, IndexError, SignatureError) as e:
        raise ledger.LedgerError(f"cannot use public key {path}: {e}") from e


def _parse_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw  # bare strings like `venue=kraken`


def cmd_init(args) -> int:
    base = Path(args.dir)
    gpath = base / "genesis.json"
    if gpath.exists():
        raise ledger.LedgerError(f"{gpath} already exists; the genesis is written once")
    genesis = {
        "schema_version": ledger.SCHEMA_VERSION,
        "type": "genesis",
        "roster": {rules.FOUNDER: {"operator": args.operator, "keys": [_read_key(args.key)]}},
        "note": "Founding roster. Seats are filled by council_change mandates.",
    }
    ledger.write_canonical(gpath, genesis)
    print(f"wrote {gpath}")
    print(f"genesis hash: {ledger.digest(genesis)}  <- every operator pins this out of band")
    print("SUN signs it:  " + SIGN_HINT.format(path=gpath))
    return 0


def cmd_propose(args) -> int:
    base = Path(args.dir)
    changes = {}
    for item in args.set or []:
        path, sep, raw = item.partition("=")
        if not sep:
            raise ledger.LedgerError(f"--set expects path=value, got {item!r}")
        changes[path] = _parse_value(raw)
    if args.seat:
        if not (args.operator and args.key):
            raise ledger.LedgerError("--seat needs --operator and --key")
        changes[f"seats.{args.seat}"] = {"operator": args.operator, "keys": [_read_key(args.key)]}
    for seat in args.vacate or []:
        changes[f"seats.{seat}"] = None

    mid, prev, seal = ledger.next_mandate_id(base)
    m = {
        "schema_version": ledger.SCHEMA_VERSION,
        "id": mid,
        "prev": prev,
        "class": args.cls,
        "title": args.title,
        "rationale": args.rationale,
        "changes": changes,
        "drafted_with_llm": args.llm,
    }
    if seal:
        m["sealed_votes"] = seal
    if args.target:
        m["target"] = args.target
    ledger._check_mandate(m, "proposal")
    ledger._validate_changes(m, ledger.default_params())
    path = base / "mandates" / f"{mid}.json"
    ledger.write_canonical(path, m)
    print(f"wrote {path}\nmandate hash: {ledger.digest(m)}")
    if seal:
        print(f"voting on {int(mid) - 1:04d} is now closed (votes sealed)")
    print(f"operators vote with: python3 -m sats_engine gov vote {mid} --seat <SEAT> --vote yes|no|abstain|veto")
    return 0


def cmd_vote(args) -> int:
    base = Path(args.dir)
    mpath = base / "mandates" / f"{args.id}.json"
    m = ledger.read_canonical(mpath)
    vote = {
        "schema_version": ledger.SCHEMA_VERSION,
        "mandate": args.id,
        "mandate_hash": ledger.digest(m),
        "seat": args.seat,
        "vote": args.vote,
    }
    path = base / "votes" / args.id / f"{args.seat}.json"
    ledger.write_canonical(path, vote)
    print(f"{args.seat} votes {args.vote.upper()} on {args.id}: {m['title']}")
    print("sign it:  " + SIGN_HINT.format(path=path))
    return 0


def cmd_status(args) -> int:
    state = ledger.load(args.dir, SSHVerifier(), args.expect_genesis)
    if args.json:
        print(json.dumps({
            "genesis": state.genesis_hash, "head": state.head, "mode": state.mode,
            "halted": state.halted, "review_required": state.review_required,
            "founding": state.founding,
            "seated": sorted(state.seated), "params": state.params,
            "mandates": [{"id": r.id, "class": r.cls, "title": r.title, "applied": r.applied,
                          "reason": r.decision.reason, "votes": r.votes, "dropped": r.dropped_votes}
                         for r in state.records],
        }, indent=2))
        return 0
    print(f"genesis {state.genesis_hash}\nhead    {state.head}")
    print(f"mode {state.mode.upper()}   halted {'YES' if state.halted else 'no'}   "
          f"review required {'YES' if state.review_required else 'no'}")
    print(f"seated {len(state.seated)}/12: " + ", ".join(
        f"{s}={state.roster[s]['operator']}" for s in rules.SEATS if state.roster[s]))
    if state.founding:
        print("FOUNDING PHASE: SUN appoints operators; nothing else passes until all 12 seats are filled")
    for r in state.records:
        mark = "APPLIED " if r.applied else "rejected"
        llm = " [llm-drafted]" if r.mandate.get("drafted_with_llm") else ""
        print(f"  {r.id} {mark} {r.cls:<14} {r.title}{llm}\n         {r.decision.reason}")
        for d in r.dropped_votes:
            print(f"         dropped vote - {d}")
    return 0


def register(sub) -> None:
    gov = sub.add_parser("gov", help="signed council mandates (Hashira 12)")
    gsub = gov.add_subparsers(dest="gov_cmd", required=True)

    def with_dir(p):
        p.add_argument("--dir", default="governance", help="ledger directory (default: governance)")
        return p

    p = with_dir(gsub.add_parser("init", help="write the founding genesis (SUN only)"))
    p.add_argument("--operator", required=True, help="SUN operator's name")
    p.add_argument("--key", required=True, help="SUN's public key file (.pub)")
    p.set_defaults(fn=cmd_init)

    p = with_dir(gsub.add_parser("propose", help="draft a new mandate"))
    p.add_argument("--class", dest="cls", required=True, choices=rules.CLASSES)
    p.add_argument("--title", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--set", action="append", metavar="PATH=VALUE", help="parameter change (repeatable)")
    p.add_argument("--seat", choices=rules.SEATS, help="council_change: seat to fill")
    p.add_argument("--operator", help="council_change: operator name for --seat")
    p.add_argument("--key", help="council_change: operator's public key file for --seat")
    p.add_argument("--vacate", action="append", choices=rules.SEATS, help="council_change: empty a seat")
    p.add_argument("--target", help="override: id of the vetoed mandate")
    p.add_argument("--llm", action="store_true", help="mark as drafted with an LLM (transparency)")
    p.set_defaults(fn=cmd_propose)

    p = with_dir(gsub.add_parser("vote", help="write a vote file for an operator to sign"))
    p.add_argument("id")
    p.add_argument("--seat", required=True, choices=rules.SEATS)
    p.add_argument("--vote", required=True, choices=rules.VOTES)
    p.set_defaults(fn=cmd_vote)

    p = with_dir(gsub.add_parser("status", help="verify every signature and show the governed state"))
    p.add_argument("--expect-genesis", help="pinned genesis hash; refuse to load anything else")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)


def run(args) -> int:
    try:
        return args.fn(args)
    except (ledger.LedgerError, SignatureError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
