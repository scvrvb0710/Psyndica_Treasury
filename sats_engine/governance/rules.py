"""Hashira 12 voting rules as code.

Pure functions only: no I/O, no crypto. Votes arriving here have already
had their signatures verified by the ledger.

Seats are held by human operators. Operators may use an LLM to draft and
analyse, but an LLM never holds a seat key, so it can never vote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEATS = (
    "FLAME", "WATER", "THUNDER", "STONE", "WIND", "MIST",
    "SERPENT", "INSECT", "SOUND", "LOVE", "MOON", "SUN",
)
VETO_SEATS = ("WATER", "STONE")  # absolute veto; only SUN can lift it, in writing
FOUNDER = "SUN"
QUORUM = 8
SUPERMAJORITY = 9
MIN_OVERRIDE_RATIONALE = 20

VOTES = ("yes", "no", "abstain", "veto")

ORDINARY = "ordinary"
CLASSES = (
    ORDINARY,
    "capital_change",
    "new_strategy",
    "new_venue",
    "council_change",
    "go_live",
    "halt",
    "restart",
    "override",
)
SUPERMAJORITY_CLASSES = ("capital_change", "new_strategy", "new_venue", "council_change", "go_live")


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    tally: dict = field(default_factory=dict)
    vetoed_by: tuple = ()


def tally(votes: dict[str, str]) -> dict[str, int]:
    t = {v: 0 for v in VOTES}
    for v in votes.values():
        t[v] += 1
    return t


def in_founding_phase(seated: set[str], constituted: bool) -> bool:
    """Founding lasts until all 12 seats have been filled once, and returns if
    the council shrinks below a supermajority - otherwise a council that can
    no longer reach 9 votes could never refill its own seats."""
    return not constituted or len(seated) < SUPERMAJORITY


def evaluate(
    cls: str,
    votes: dict[str, str],
    seated: set[str],
    rationale: str = "",
    ignore_vetoes: bool = False,
    founding: bool = False,
) -> Decision:
    """Decide one mandate. `votes` maps seat -> vote for verified, seated voters."""
    votes = dict(votes)
    notes = []
    for seat, v in list(votes.items()):
        if v == "veto" and seat not in VETO_SEATS:
            notes.append(f"{seat} has no veto; counted as no")
            votes[seat] = "no"
    t = tally(votes)
    note = f" ({'; '.join(notes)})" if notes else ""

    def decide(ok: bool, reason: str, vetoed_by: tuple = ()) -> Decision:
        return Decision(ok, reason + note, t, vetoed_by)

    # Halting is the safe direction: any one seated operator can do it alone.
    if cls == "halt":
        return decide(t["yes"] >= 1, "halt by single seat" if t["yes"] else "halt needs one yes")

    if founding:
        # The founder appoints the council; nothing else can pass until it exists.
        if cls != "council_change":
            return decide(False, f"founding phase ({len(seated)}/{len(SEATS)} seated): only council_change allowed")
        ok = votes.get(FOUNDER) == "yes"
        return decide(ok, "founding phase: seated by SUN" if ok else "founding phase: needs SUN yes")

    if cls == "override":
        if votes.get(FOUNDER) != "yes":
            return decide(False, "override needs SUN yes")
        if len(rationale.strip()) < MIN_OVERRIDE_RATIONALE:
            return decide(False, f"override needs a written reason (>= {MIN_OVERRIDE_RATIONALE} chars)")
        return decide(True, "SUN override in writing")

    vetoes = tuple(s for s in VETO_SEATS if votes.get(s) == "veto")
    if vetoes and not ignore_vetoes:
        return decide(False, f"vetoed by {', '.join(vetoes)}", vetoes)

    cast = sum(t.values())
    if cast < QUORUM:
        return decide(False, f"no quorum ({cast}/{QUORUM})")

    if cls in SUPERMAJORITY_CLASSES:
        if t["yes"] < SUPERMAJORITY:
            return decide(False, f"needs {SUPERMAJORITY} yes, got {t['yes']}")
        if cls == "go_live" and votes.get(FOUNDER) != "yes":
            return decide(False, "go_live needs SUN yes")
        return decide(True, f"supermajority {t['yes']}/{len(SEATS)}")

    # ordinary and restart: simple majority of yes/no among a quorate vote
    decided = t["yes"] + t["no"] + t["veto"]
    if t["yes"] * 2 > decided:
        return decide(True, f"majority {t['yes']} yes / {decided}")
    return decide(False, f"no majority ({t['yes']} yes / {decided})")
