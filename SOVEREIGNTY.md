# Psyndica Sovereignty Roadmap

**For:** the Hashira 12 council · **Status:** draft for adoption ·
**Owner:** FLAME (agenda), SUN (final) · **Red team:** MOON

> Sovereignty means no single vendor, person, or server can stop us or take
> from us. It does not mean self-hosting everything.

## Decisions requested
1. **Adopt this roadmap** as the council's sequence of work.
2. **Confirm reserve custody.** Who holds the treasury BTC keys today,
   and how? This is not recorded anywhere, and it is the highest-risk
   unknown.
3. **Nominate operators** for the 11 open seats, so the founding phase can
   end.

*Limitation:* the signed-mandate ledger currently records engine parameters
and seat changes only. Adopting a roadmap is a non-parameter decision, so
until a `resolution` mandate class exists (proposed in Phase 0), record it
in a signed commit that names the voting operators.

## How we choose what to take back

Three tests, applied in order:

1. **Irreversibility first.** Fix what can't be undone (lost keys, a seized
   reserve, a forged decision) before what is merely inconvenient (a dead
   VPS, a banned Slack account).
2. **Local-first, cloud as a replica.** Our own copy is the source of truth.
   Vendors hold copies and provide convenience, never the only copy.
3. **Self-host only what we can maintain.** Every service we run needs a
   named owner and a patching schedule. An unpatched self-hosted service is
   worse than a vendor (see the PHP compromise).

## Dependency register (today)

| Layer | Depends on | If it fails | Reversible? | Exit plan |
|---|---|---|---|---|
| Reserve keys | **unconfirmed** | stack lost | **no** | Phase 0: multisig across people and places |
| Governance | signed mandates built; genesis not yet created | decisions forgeable or ignored | partly | Phase 0: genesis, 12 seats, pinned hash; Phase 4: anchored on Cardano |
| Verification | VPS provider (pruned Knots, planned) | blind to chain data | yes | Phase 1: own hardware, second node |
| Chain data history | VPS local CSV, with a Cloudflare D1 replica | gap in history | yes | already local-first; Phase 1 adds a second replica we own |
| Swarm venue | Coinbase AgentKit plus a perp venue | positions frozen | capped at $500 | accepted by design; Stone isolation |
| Payments in | Stripe (installed), Fedi | deplatformed | yes | Phase 2: BTCPay plus Lightning |
| Code | GitHub | lose forge and CI | yes | Phase 3: self-hosted forge mirror, signed commits |
| Comms | Slack, Google | lose coordination | yes | Phase 3: Matrix or Nostr, email on own domain |
| Agents | Claude and connectors | lose automation | yes | everything agents build lands as code in the repo; least-privilege connectors |
| Legal | none formalized | personal liability, unclear authority | — | parallel track: counsel on an entity wrapper |

## Phase 0: Founding (now)
*Goal: nothing irreversible depends on one person or one secret.*

| Work | Owner | Done when |
|---|---|---|
| Reserve into multisig (e.g. 2-of-3 or 3-of-5) with keys held by different humans in different places; seeds on metal | LOVE + WATER, SUN signs | recovery drill passes without exposing a seed |
| Create the governance genesis on a hardware key; publish its hash | SUN | every operator has the hash recorded outside the repo |
| Seat all 12 operators, each with a hardware seat key | SUN (founding phase) | `gov status` shows 12/12, founding phase over |
| GitHub: protected `main`, signed commits required, secret scanning and push protection | STONE | settings verified by a second operator |
| Add a `resolution` mandate class for non-parameter decisions like this one | SERPENT drafts, council votes | decisions recorded in the ledger, not in chat |
| Monthly connector and access review (SECURITY.md) | STONE | first review logged |

**Agents never hold reserve keys or seat keys.** Operators guide the LLM;
humans sign.

## Phase 1: Verify
*Goal: we check the chain ourselves, in more than one place.*

| Work | Owner | Done when |
|---|---|---|
| Move Knots from VPS to our own hardware (2 TB NVMe, full node) | MIST | node synced on our hardware, VPS retired |
| Second node run by a different operator in a different jurisdiction | MIST + another seat | both nodes agree on tip height |
| Node peer traffic over Tor | STONE | no clearnet inbound to the node |
| Deploy the collector, then add a second replica on hardware we own | MIST + SOUND | stale alerts work; a restore from either replica is tested |

## Phase 2: Transact
*Goal: we receive and send BTC without asking anyone's permission.*

| Work | Owner | Done when |
|---|---|---|
| Lightning node on our hardware | THUNDER | channels open, balances reconciled by INSECT |
| BTCPay Server for receiving payments; uninstall Stripe connector | INSECT + LOVE | first real payment settles to our own node |
| Fedi for community and artist payments only, with a sweep schedule to the reserve | LOVE | balances stay under a council-set cap |

## Phase 3: Coordinate
*Goal: losing a Big Tech account costs convenience, not continuity.*

| Work | Owner | Done when |
|---|---|---|
| Self-hosted forge mirror (e.g. Forgejo); GitHub becomes the public mirror | STONE | a push to either reaches both; restore tested |
| Team comms on Matrix or Nostr | FLAME | council business runs there for a month |
| Email on our own domain, protected like Tier 0 (it is the recovery path) | STONE | registrar and DNS on hardware-key 2FA |

## Phase 4: Anchor
*Goal: our governance record can't be quietly truncated or rewritten.
"ADA executes" and "NIGHT conceals" become real.*

| Work | Owner | Done when |
|---|---|---|
| Publish the ledger head hash on Cardano after each accepted mandate | SERPENT + THUNDER | anyone can check the repo's head against the chain (fixes truncation) |
| Move mandate voting to Cardano scripts, once off-chain voting has run cleanly | council (9 of 12) | one full mandate cycle on-chain |
| Private votes and payments on Midnight where privacy matters | STONE + SERPENT | design reviewed by MOON before any build |

## Parallel track: legal wrapper
Self-governance on-chain still needs an entity off-chain, for contracts,
taxes, and liability. Options exist, such as Wyoming's DAO LLC or DUNA
structures. **Counsel decides, not this document.** Owner: SUN with
INSECT.

## Moon's objection
- **More self-hosting means more attack surface.** Phases 1–3 each add
  services. Each needs a named owner and patch cadence before it starts,
  or it doesn't start.
- **Multisig adds recovery risk.** Too many lost keys and the reserve is
  locked forever. The quarterly recovery drill is not optional.
- **Twelve operators is a real staffing burden.** Quorum needs eight
  people who are actually available. An under-staffed council stalls, and
  falls back into the founding phase, which means trusting Sun.
- **On-chain governance can't be undone.** Mistakes become permanent and
  public. Run Phase 4 only after off-chain governance has proven itself.
- **Sovereignty can become a distraction.** None of this grows the stack
  by itself. Keep each phase small and finish it before starting the next.

## How we measure it
Reviewed quarterly by the council:

- **Irreversible single points of failure:** target zero. Today: reserve
  custody is unconfirmed.
- **Critical layers with a tested exit plan:** count from the register
  above.
- **Recovery drills passed:** reserve, collector restore, forge restore.
- **Seats filled:** 1 of 12 today, once genesis is created.
- **Time from proposal to mandate:** governance should be deliberate, not
  stalled.
