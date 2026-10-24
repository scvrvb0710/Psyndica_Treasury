"""Signed-mandate ledger: the council's decisions as a hash chain.

Layout (all JSON in canonical form: sorted keys, no spaces, UTF-8):

    governance/
      genesis.json                 founding roster (+ optional param overrides)
      genesis.json.sig             SUN's signature over genesis.json
      mandates/0001.json ...       proposals, each linking to the previous by hash
      votes/0001/WATER.json        one vote per seat per mandate
      votes/0001/WATER.json.sig    that operator's ssh signature over the vote file

Voting on a mandate closes when the next one is proposed: the new mandate
carries `sealed_votes`, a hash of every vote file on the previous one. After
that, deleting a vote (to un-pass a mandate) or adding a late one (to pass
it retroactively) breaks the ledger instead of silently rewriting history.

Replaying the ledger yields the governed state: who holds which seat, every
engine parameter, shadow/live mode, and whether the swarm is halted. Any
structural fault (broken chain, malformed file, bad genesis signature)
fails the whole ledger closed. A bad or missing vote is only dropped.

Trust root: genesis.json. Anyone who can rewrite the repo could swap in a
new genesis, so operators must pin the genesis hash out of band and pass
it as `expect_genesis` (CLI: --expect-genesis).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from ..config import EngineConfig
from . import rules
from .sshsig import SignatureError, normalize_key

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^\d{4}$")


class LedgerError(RuntimeError):
    pass


class Verifier(Protocol):
    def verify(self, message: bytes, signature: bytes, principal: str, pubkeys: list[str]) -> bool: ...


# ---------------------------------------------------------------- canonical


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def read_canonical(path: Path) -> dict:
    """Load a JSON file that must already be in canonical form.

    Signatures cover the exact bytes, so a file that isn't canonical could
    mean two readers see different content under one signature. Reject it.
    """
    raw = path.read_bytes()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerError(f"{path.name}: invalid JSON") from e
    if not isinstance(obj, dict) or canonical(obj) != raw.rstrip(b"\n"):
        raise LedgerError(f"{path.name}: not canonical JSON (write it with the gov CLI)")
    return obj


def write_canonical(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(obj))


def _closed(obj: dict, required: set, optional: set, where: str) -> None:
    keys = set(obj)
    if missing := required - keys:
        raise LedgerError(f"{where}: missing {sorted(missing)}")
    if unknown := keys - required - optional:
        raise LedgerError(f"{where}: unknown fields {sorted(unknown)}")


# ------------------------------------------------------------ param registry

CAPITAL_PATHS = {
    "capital_usd", "engine.sleeve_frac", "engine.max_open_total", "engine.halt_drawdown",
    "fees.edge_fee_multiple", "fees.min_edge_abs", "swing.max_hold_bars", "scalp.max_hold_bars",
    "swing.size_frac", "scalp.size_frac", "swing.max_open", "scalp.max_open",
    "dca.contribution_usd", "dca.max_mult",
}
STRATEGY_PATHS = {"swing.enabled", "scalp.enabled"}
FRACTION_PATHS = {"engine.sleeve_frac", "engine.halt_drawdown", "swing.size_frac", "scalp.size_frac"}
EXTRA_DEFAULTS = {"capital_usd": 500.0, "mode": "shadow", "venue": "none"}
MODES = ("shadow", "live")


def default_params() -> dict[str, Any]:
    """Flatten EngineConfig into governable dotted paths."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(EngineConfig):
        value = getattr(EngineConfig(), f.name)
        if dataclasses.is_dataclass(value):
            for sub in dataclasses.fields(value):
                out[f"{f.name}.{sub.name}"] = getattr(value, sub.name)
        else:
            out[f"engine.{f.name}"] = value
    out.update(EXTRA_DEFAULTS)
    return out


def required_class(path: str, value: Any) -> str:
    if path in CAPITAL_PATHS:
        return "capital_change"
    if path in STRATEGY_PATHS:
        return "new_strategy"
    if path == "venue":
        return "new_venue"
    if path == "mode":
        # Going live takes the full go-live vote; stepping back to shadow is safe.
        return "go_live" if value == "live" else rules.ORDINARY
    return rules.ORDINARY


def validate_param(path: str, value: Any, defaults: dict[str, Any]) -> None:
    if path not in defaults:
        raise LedgerError(f"unknown parameter {path!r}")
    d = defaults[path]
    if isinstance(d, bool):
        ok = isinstance(value, bool)
    elif isinstance(d, int):
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    elif isinstance(d, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    else:
        ok = isinstance(value, str) and 0 < len(value) <= 64
    if not ok:
        raise LedgerError(f"{path}: {value!r} is not a valid {type(d).__name__}")
    if path in FRACTION_PATHS and not 0 < value <= 1:
        raise LedgerError(f"{path}: must be in (0, 1]")
    if path == "mode" and value not in MODES:
        raise LedgerError(f"mode must be one of {MODES}")
    if isinstance(d, int) and not isinstance(d, bool) and value < 1:
        raise LedgerError(f"{path}: must be >= 1")


def governed_config(params: dict[str, Any]) -> EngineConfig:
    cfg = EngineConfig()
    for path, value in params.items():
        head, _, name = path.partition(".")
        if head == "engine":
            setattr(cfg, name, value)
        elif name and hasattr(cfg, head):
            setattr(getattr(cfg, head), name, value)
    return cfg


# ------------------------------------------------------------------ roster


def validate_seat_entry(seat: str, entry: Any) -> Optional[dict]:
    if seat not in rules.SEATS:
        raise LedgerError(f"unknown seat {seat!r}")
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise LedgerError(f"seat {seat}: entry must be an object or null")
    _closed(entry, {"operator", "keys"}, set(), f"seat {seat}")
    if not isinstance(entry["operator"], str) or not 0 < len(entry["operator"]) <= 64:
        raise LedgerError(f"seat {seat}: bad operator name")
    if not isinstance(entry["keys"], list) or not 1 <= len(entry["keys"]) <= 4:
        raise LedgerError(f"seat {seat}: needs 1-4 public keys")
    try:
        keys = [normalize_key(k) for k in entry["keys"]]
    except SignatureError as e:
        raise LedgerError(f"seat {seat}: {e}") from e
    return {"operator": entry["operator"], "keys": keys}


# ------------------------------------------------------------------- state


@dataclass
class Record:
    id: str
    hash: str
    cls: str
    title: str
    decision: rules.Decision
    votes: dict[str, str]
    dropped_votes: list[str]
    applied: bool = False
    overridden_by: Optional[str] = None
    mandate: dict = field(default_factory=dict, repr=False)


@dataclass
class State:
    genesis_hash: str
    head: str
    roster: dict[str, Optional[dict]]
    params: dict[str, Any]
    halted: bool = False
    review_required: bool = False  # set by a SUN override: council reviews the next trade
    constituted: bool = False  # all 12 seats have been filled at least once
    records: list[Record] = field(default_factory=list)

    @property
    def seated(self) -> set[str]:
        return {s for s, e in self.roster.items() if e}

    @property
    def founding(self) -> bool:
        return rules.in_founding_phase(self.seated, self.constituted)

    @property
    def mode(self) -> str:
        return self.params["mode"]

    def config(self) -> EngineConfig:
        return governed_config(self.params)

    def require_live(self) -> None:
        """Gate for any future executor: raise unless the council voted live."""
        if self.halted:
            raise LedgerError("swarm is HALTED by mandate; restart needs a council vote")
        if self.mode != "live":
            raise LedgerError("mode is shadow; going live needs a go_live mandate (9 of 12 + SUN)")


# ------------------------------------------------------------------ replay

MANDATE_REQUIRED = {"schema_version", "id", "prev", "class", "title", "rationale", "changes"}
MANDATE_OPTIONAL = {"target", "drafted_with_llm", "sealed_votes"}
VOTE_FIELDS = {"schema_version", "mandate", "mandate_hash", "seat", "vote"}


def _check_mandate(m: dict, where: str) -> None:
    _closed(m, MANDATE_REQUIRED, MANDATE_OPTIONAL, where)
    if m["schema_version"] != SCHEMA_VERSION:
        raise LedgerError(f"{where}: unsupported schema_version")
    if m["class"] not in rules.CLASSES:
        raise LedgerError(f"{where}: unknown class {m['class']!r}")
    for k in ("title", "rationale"):
        if not isinstance(m[k], str) or len(m[k]) > 4000:
            raise LedgerError(f"{where}: bad {k}")
    if not isinstance(m["changes"], dict):
        raise LedgerError(f"{where}: changes must be an object")
    if "drafted_with_llm" in m and not isinstance(m["drafted_with_llm"], bool):
        raise LedgerError(f"{where}: drafted_with_llm must be true/false")


def _validate_changes(m: dict, defaults: dict) -> None:
    """Raise if this mandate's changes are not allowed for its class."""
    cls, changes = m["class"], m["changes"]
    if cls in ("halt", "restart"):
        if changes:
            raise LedgerError(f"{cls} carries no changes")
        return
    if cls == "override":
        if changes or not isinstance(m.get("target"), str) or not ID_RE.match(m["target"]):
            raise LedgerError("override needs a target mandate id and no changes")
        return
    if "target" in m:
        raise LedgerError("only overrides have a target")
    if not changes:
        raise LedgerError("no changes")
    for path, value in changes.items():
        if cls == "council_change":
            if not path.startswith("seats."):
                raise LedgerError("council_change may only change seats.*")
            validate_seat_entry(path[len("seats."):], value)
            continue
        if path.startswith("seats."):
            raise LedgerError("seat changes need class council_change")
        validate_param(path, value, defaults)
        need = required_class(path, value)
        if need not in (rules.ORDINARY, cls):
            raise LedgerError(f"{path} needs class {need}, not {cls}")


def _apply(state: State, m: dict) -> None:
    cls = m["class"]
    if cls == "halt":
        state.halted = True
    elif cls == "restart":
        state.halted = False
    elif cls == "council_change":
        for path, value in m["changes"].items():
            seat = path[len("seats."):]
            state.roster[seat] = validate_seat_entry(seat, value)
        if len(state.seated) == len(rules.SEATS):
            state.constituted = True
    else:
        state.params.update(m["changes"])


def vote_seal(base: Path, mid: str) -> str:
    """Hash of every file in votes/<mid>/ (valid or not), so any later
    addition, removal, or edit is detectable."""
    vdir = Path(base) / "votes" / mid
    files = sorted(vdir.iterdir()) if vdir.is_dir() else []
    return digest([[f.name, hashlib.sha256(f.read_bytes()).hexdigest()] for f in files if f.is_file()])


def _load_votes(base: Path, m: dict, mhash: str, state: State, verifier: Verifier) -> tuple[dict, list]:
    votes, dropped = {}, []
    vdir = base / "votes" / m["id"]
    if not vdir.is_dir():
        return votes, dropped
    for vfile in sorted(vdir.glob("*.json")):
        seat = vfile.stem
        try:
            v = read_canonical(vfile)
            _closed(v, VOTE_FIELDS, set(), vfile.name)
            if v["seat"] != seat or v["mandate"] != m["id"] or v["mandate_hash"] != mhash:
                raise LedgerError("vote does not match this mandate/seat")
            if v["schema_version"] != SCHEMA_VERSION or v["vote"] not in rules.VOTES:
                raise LedgerError("bad vote")
            entry = state.roster.get(seat)
            if not entry:
                raise LedgerError(f"{seat} is not seated")
            sig = vfile.with_name(vfile.name + ".sig")
            if not sig.exists():
                raise LedgerError("unsigned")
            if not verifier.verify(vfile.read_bytes(), sig.read_bytes(), seat, entry["keys"]):
                raise LedgerError("signature invalid for seat key")
            votes[seat] = v["vote"]
        except (LedgerError, SignatureError) as e:
            dropped.append(f"{seat}: {e}")
    return votes, dropped


def load(base: str | Path, verifier: Verifier, expect_genesis: Optional[str] = None) -> State:
    base = Path(base)
    gpath = base / "genesis.json"
    if not gpath.exists():
        raise LedgerError(f"no genesis.json in {base}")
    genesis = read_canonical(gpath)
    _closed(genesis, {"schema_version", "type", "roster"}, {"params", "note"}, "genesis")
    if genesis["type"] != "genesis" or genesis["schema_version"] != SCHEMA_VERSION:
        raise LedgerError("genesis: bad type/schema_version")
    ghash = digest(genesis)
    if expect_genesis and ghash != expect_genesis:
        raise LedgerError(f"genesis hash {ghash} does not match the pinned {expect_genesis}")

    roster = {s: None for s in rules.SEATS}
    if not isinstance(genesis["roster"], dict):
        raise LedgerError("genesis: roster must be an object")
    for seat, entry in genesis["roster"].items():
        roster[seat] = validate_seat_entry(seat, entry)
    if not roster[rules.FOUNDER]:
        raise LedgerError("genesis: SUN must be seated")

    defaults = default_params()
    params = dict(defaults)
    for path, value in (genesis.get("params") or {}).items():
        validate_param(path, value, defaults)
        params[path] = value
    if params["mode"] != "shadow":
        raise LedgerError("genesis must start in shadow mode")

    sig = gpath.with_name("genesis.json.sig")
    if not sig.exists() or not verifier.verify(gpath.read_bytes(), sig.read_bytes(), rules.FOUNDER,
                                               roster[rules.FOUNDER]["keys"]):
        raise LedgerError("genesis is not validly signed by SUN")

    state = State(genesis_hash=ghash, head=ghash, roster=roster, params=params)
    state.constituted = len(state.seated) == len(rules.SEATS)
    by_id: dict[str, Record] = {}
    files = sorted((base / "mandates").glob("*.json")) if (base / "mandates").is_dir() else []
    for n, mfile in enumerate(files, start=1):
        m = read_canonical(mfile)
        where = mfile.name
        _check_mandate(m, where)
        if m["id"] != f"{n:04d}" or mfile.stem != m["id"]:
            raise LedgerError(f"{where}: mandates must be numbered 0001, 0002, ... without gaps")
        if m["prev"] != state.head:
            raise LedgerError(f"{where}: chain broken (prev does not match the previous mandate)")
        if n > 1:
            prev_id = f"{n - 1:04d}"
            if m.get("sealed_votes") != vote_seal(base, prev_id):
                raise LedgerError(f"{where}: votes on {prev_id} changed after they were sealed")
        elif "sealed_votes" in m:
            raise LedgerError(f"{where}: the first mandate has no previous votes to seal")
        mhash = digest(m)
        state.head = mhash

        votes, dropped = _load_votes(base, m, mhash, state, verifier)
        try:
            _validate_changes(m, defaults)
            decision = rules.evaluate(m["class"], votes, state.seated, m["rationale"], founding=state.founding)
        except LedgerError as e:
            decision = rules.Decision(False, f"invalid: {e}", rules.tally(votes))

        rec = Record(m["id"], mhash, m["class"], m["title"], decision, votes, dropped, mandate=m)
        if decision.accepted and m["class"] == "override":
            target = by_id.get(m["target"])
            if not target or not target.decision.vetoed_by or target.applied:
                rec.decision = rules.Decision(False, "override target must be an earlier vetoed mandate",
                                              decision.tally)
            else:
                # Re-decide the target with vetoes lifted. It still needs its own
                # quorum and threshold - an override lifts a veto, nothing more.
                redo = rules.evaluate(target.cls, target.votes, state.seated, target.mandate["rationale"],
                                      ignore_vetoes=True, founding=state.founding)
                target.overridden_by = m["id"]
                state.review_required = True
                if redo.accepted:
                    _apply(state, target.mandate)
                    target.applied = True
                    target.decision = rules.Decision(True, f"{redo.reason}; veto lifted by SUN override {m['id']}",
                                                     redo.tally)
        elif decision.accepted:
            _apply(state, m)
            rec.applied = True
        state.records.append(rec)
        by_id[m["id"]] = rec
    return state


# --------------------------------------------------------------- authoring


def next_mandate_id(base: str | Path) -> tuple[str, str, Optional[str]]:
    """(next id, prev hash, seal of the previous mandate's votes) for a new
    proposal. Proposing closes voting on the previous mandate."""
    base = Path(base)
    files = sorted((base / "mandates").glob("*.json")) if (base / "mandates").is_dir() else []
    if not files:
        return "0001", digest(read_canonical(base / "genesis.json")), None
    return f"{len(files) + 1:04d}", digest(read_canonical(files[-1])), vote_seal(base, files[-1].stem)
