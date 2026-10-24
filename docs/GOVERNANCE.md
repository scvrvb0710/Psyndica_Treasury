# Signed mandates: Hashira 12 governance in code

The council's rules are enforced by the engine, not by an agent agreeing to
follow a prompt. A parameter, a go-live, or a seat change takes effect only
when enough operators have signed votes for it with their own keys.

## Who holds a seat

Each of the 12 seats is held by a **human operator**. Operators guide LLMs:
they use them to draft proposals, run backtests, and argue the red-team
case. **An LLM never holds a seat key, so it can never vote.** A mandate
drafted with an LLM is flagged `drafted_with_llm`, and `gov status` shows
it as `[llm-drafted]`, so the council always knows what it's signing.

| Seat | Role | Special power in code |
|---|---|---|
| FLAME | Chair | — |
| WATER | Risk / Health | **absolute veto** |
| STONE | Isolation Guardian | **absolute veto** |
| SOUND | Watcher | can halt alone (as can every seat) |
| SUN | Founder | seats the founding council; go_live needs SUN yes; override lifts a veto, in writing |
| THUNDER, WIND, MIST, SERPENT, INSECT, LOVE, MOON | — | one vote each |

## Rules

| Class | Needs |
|---|---|
| `ordinary` (strategy tuning) | quorum 8, majority of yes/no |
| `capital_change` (caps, sizing, edge floor, hold cap, halt level) | quorum 8, **9 yes** |
| `new_strategy` (enable/disable a leg) | quorum 8, **9 yes** |
| `new_venue` | quorum 8, **9 yes** |
| `council_change` (seat/vacate/rotate keys) | quorum 8, **9 yes** |
| `go_live` (`mode=live`) | quorum 8, **9 yes including SUN** |
| `halt` | **any one seated operator** |
| `restart` | quorum 8, majority |
| `override` | SUN yes plus a written reason of 20+ characters; lifts WATER/STONE vetoes on one earlier mandate, which must still meet its own threshold. Sets `review_required` |

- **Vetoes:** a WATER or STONE veto blocks anything except halt and
  override. A veto from any other seat counts as a no.
- **Class must match the change:** you can't change `engine.sleeve_frac`
  with an `ordinary` mandate. Unknown parameters are rejected.
- **Founding phase:** until all 12 seats have been filled once, only SUN
  can pass mandates, and only `council_change` (appointing operators). The
  founding phase returns if the council ever shrinks below 9, so it can
  always rebuild itself.

## Operator setup (once)

1. Make a seat key, ideally on a hardware security key:
   ```bash
   ssh-keygen -t ed25519-sk -C "WATER psyndica"   # YubiKey etc.; or -t ed25519 as a fallback
   ```
2. Send SUN the **public** key (`.pub`) only. The private key never leaves
   your device.
3. **Pin the genesis hash.** Write down the hash SUN publishes when
   creating the ledger, somewhere outside this repo (your password
   manager). Always run with `--expect-genesis <hash>`.

## Workflow

```bash
# Any operator (or an LLM they direct) drafts:
python3 -m sats_engine gov propose --class capital_change --title "Sleeve to 20%" \
    --rationale "Shadow results show ..." --set engine.sleeve_frac=0.2 [--llm]

# Each operator, on their own machine:
python3 -m sats_engine gov vote 0007 --seat WATER --vote yes      # or no / abstain / veto
ssh-keygen -Y sign -n psyndica-mandate -f ~/.ssh/id_ed25519_sk governance/votes/0007/WATER.json
git add governance/votes/0007/WATER.json* && git commit -S -m "WATER votes on 0007"

# Anyone verifies every signature and sees the result:
python3 -m sats_engine gov status --expect-genesis <hash>

# Engine runs take every parameter from the ledger; CLI overrides are refused:
python3 -m sats_engine backtest --prices btc_1h.csv --mandates governance --expect-genesis <hash>
```

**Voting closes when the next mandate is proposed.** The new mandate seals
a hash of the previous one's vote files. Collect every vote before
proposing the next mandate.

Founding: SUN runs `gov init --operator <name> --key sun.pub`, signs
`genesis.json`, then seats operators with
`gov propose --class council_change --seat WATER --operator alice --key alice.pub`
and votes yes.

## What it protects against (tested)
- **Forged votes:** each signature is checked against that seat's key at
  that point in history.
- **Editing a voted mandate:** its votes no longer match its hash and are
  dropped.
- **Rewriting history:** every mandate links to the previous one by hash.
- **Deleting or adding votes after the fact:** sealed by the next mandate.
- **A swapped genesis:** caught by the pinned hash.
- **Non-canonical JSON:** rejected, so a signature always covers exactly
  one meaning.
- **Key lines carrying injected options:** rejected by strict key parsing.
- **Sneaking a capital change through as an ordinary mandate:** rejected.

## Limits (Moon's objection)
- **Founding is trust in SUN.** Until all 12 seats are filled, SUN alone
  shapes the council. Fill the seats quickly with people who would say no
  to Sun.
- **Truncation:** someone with repo write access can delete the *newest*
  mandates (for example a recent halt), leaving a shorter but valid chain.
  Defences: signed commits, branch protection, operators noting the `head`
  hash after each vote, and later anchoring the head hash on Cardano (step
  5 of the sovereignty plan).
- **Withholding:** a signed vote that's never committed doesn't count.
  That's a people problem, not a code one.
- **Verification needs OpenSSH** (`ssh-keygen`). Without it, nothing
  verifies, so nothing passes. The system fails closed.
- **Quorum takes people.** Twelve operators who are actually available is
  the real requirement.
