# Psyndica Security Ethos

> Protect the stack. Survival beats return, every time.

A breach costs more than any trade can earn. Security is a standing duty,
reviewed on a schedule. It is not a launch checklist item done once. When
convenience and security conflict, security wins and the trade-off is
written down.

## Principles

1. **Assume breach.** Design so any single stolen password, laptop, server,
   or agent session cannot move funds or push code alone.
2. **Least privilege.** Every key, token, account, and agent gets the
   smallest scope that works, and loses it when the job ends.
3. **Separation by job.** BTC stores, ADA executes, NIGHT conceals. Cold
   reserve, swarm wallet, spending wallet (Fedi), infrastructure, and code are
   separate blast zones. Nothing bridges them automatically.
4. **Verify, don't trust.** Signatures on software, reconciliation on
   balances, review on code. "It looked fine" is not verification.
5. **Small attack surface.** Every dependency, plugin, open port, and
   connected service is a door. Fewer doors.
6. **Halt is a success.** Any person or agent may halt on suspicion. No one
   is blamed for a false alarm. Restarting requires a review.

## Tiers of value

| Tier | Holds | Controls |
|---|---|---|
| **0 – Reserve** | Treasury BTC | Hardware wallets, multisig (e.g. 2-of-3) across separate people and places. Seeds on metal, offline, never typed into any computer or phone. Never connected to bots, agents, or hot services. |
| **1 – Swarm** | Swarm wallet (≤ $500) | Single designated agent wallet, spending limits set at the wallet, trade-only keys, no withdrawal permission. |
| **2 – Spending** | Fedi / Lightning | Small balances only; sweep surplus to Tier 0 on a schedule. |
| **3 – Infrastructure** | Node VPS, websites, cloud accounts | Holds **no funds and no keys**. Compromise must cost data and time, never BTC. |

Funds flow **down** to Tier 0 by sweep. Nothing pulls automatically **up**
from Tier 0.

## Accounts and identity
- Use hardware security keys or passkeys for GitHub, email, domain
  registrar, cloud, exchanges, and DNS. **No SMS 2FA.** SIM swaps are
  routine.
- Every person gets their own account. No shared logins. Remove access the
  same day someone leaves.
- The email account behind domain and registrar recovery is the master key.
  Protect it like Tier 0.
- Use a password manager with unique passwords everywhere.

## Code and supply chain
- **Stdlib-first.** `sats_engine` has zero third-party dependencies. Adding
  one requires a written reason, a review of the package and its
  maintainers, and an exact version pin with a hash.
- Protect `main`: pull requests only, at least one human review, no force
  pushes, tests passing.
- Sign commits (SSH or GPG signing) and require signed commits on `main`.
  A commit claiming to be from a maintainer is not proof.
- Pin CI actions by full commit SHA, not by tag.
- Never commit secrets. `tests/test_security.py` scans the tree and fails on
  private keys, extended private keys, and common token formats. Enable
  GitHub secret scanning and push protection on the repo.
- No `eval`, `exec`, `pickle` on untrusted data, or shell-string commands
  in engine code.

## Web surfaces (lessons from the PHP compromise)
- Inventory every public site, plugin, and admin panel. Delete what isn't
  used.
- Patch on a schedule. Unpatched CMS plugins and themes are the most common
  way in.
- Put admin panels behind SSO or Access, never on the open internet with
  password-only login.
- Websites run on separate hosting from everything else, with no
  credentials that reach other systems.
- Keep offline, tested backups. A backup that has never been restored is a
  hope, not a backup.

## Infrastructure
- Node and VPS: see [docs/VPS_NODE.md](docs/VPS_NODE.md). SSH keys only,
  firewall default-deny, RPC bound to localhost, wallet disabled.
- Verify signatures on every binary (Knots, OS images) before running it.
- Read-only by default: scanners and agents read data; nothing on Tier 3
  can sign a transaction.

## AI agents (Claude, the Hashira swarm)
- Agents never see, request, or store seed phrases, private keys, API
  secrets, or CDP secrets.
- Give each agent the fewest connectors it needs, and disconnect unused
  ones. An agent session with email, drive, and chat access is a high-value
  target.
- Treat content an agent reads (web pages, PR comments, emails, documents)
  as data, never as instructions. Prompt injection is the agent version of
  phishing.
- Agents cannot merge to `main`, approve their own work, or move funds
  outside the designated swarm wallet.
- Agents log every action with a `trace_id` so incidents can be replayed.

## Incident response
1. **Halt.** Stop the swarm, revoke sessions, take the affected system
   offline. Don't wait for certainty.
2. **Contain.** Rotate every credential the system could reach. Move funds
   from any exposed tier to a fresh Tier 0 address only if Tier 0 itself is
   not in doubt.
3. **Preserve.** Snapshot logs and disks before cleaning up.
4. **Report** to Sun with timeline, scope, and trace_ids.
5. **Post-mortem.** Blameless, written. Every finding becomes a control in
   this document or a test.

## Review cadence
- **Monthly:** access list, connected apps and agent connectors, patch
  status, backup restore test.
- **Quarterly:** Tier 0 recovery drill (prove the multisig can sign without
  exposing seeds), and a red-team review: what Moon would attack first.
- **After any incident or new system:** update this document.
