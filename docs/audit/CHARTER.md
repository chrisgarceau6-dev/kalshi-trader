# Audit Charter — 2026-08-24

Two agents (Claude Code and Codex) audit this project **in parallel, in separate
chats, against the same repo**. This file is the contract. Read it fully before
doing anything.

## Why this exists

The project accumulated across many terminal sessions. CLAUDE.md is the only
continuity between them, so anything not written down never happened. In the 24h
before this charter, four separate measurement bugs were found, each of which had
live decisions built on top of it:

- config table said the bet was $50; it has been $25 since #151
- break-even was computed fee-blind, showing +0.34pp green on a window that lost $64.97
- `depth==0` was read as "thin book" when it means "the price is not on offer at all"
- the markets endpoint uses `volume_fp`/`open_interest_fp`; querying `volume`/`open_interest`
  returns 0 for every series **including ones the bot is actively filling**

None of these were carelessness. They survived because exactly one thing was
checking, working from a summary, while changes shipped daily. The audit clears the
backlog. The freeze, the second auditor, and `scripts/verify.py` are what stop it
recurring.

## HARD RULES — both agents

1. **The trading path is READ-ONLY.** No edits to `late_certainty_trader.py`,
   `kalshi_auth.py`, `.github/workflows/*`, or any live constant. Not even an
   obvious fix.
2. **NOTHING live changes without Chris's explicit approval.** If you find a bug
   that is actively losing money, **flag it to Chris immediately and stop**. Do not
   fix it. He is the decision-maker; that is the point of the exercise.
3. **Stay in your own directory.**
   - Codex writes ONLY to `docs/audit/codex/`
   - Claude writes ONLY to `docs/audit/claude/` and `scripts/verify.py`
   - Neither touches anything else. Commit only your own directory; pull before push.
4. **Do NOT read the other agent's folder until the Session 2 diff.** Independence
   is the entire value. Reading the other spec first inherits its errors.
5. **No number without a command.** Any figure in any artifact must carry a
   one-liner that reproduces it. If it cannot be reproduced, it does not go in.
6. **Anything you cannot classify gets `UNKNOWN` and is surfaced.** Never silently
   bin something.

## Scope

IN: all of `~/pm`; Kalshi-related files anywhere else on the machine (notably
`~/Downloads/Finance/` — the repo holds the OLDEST `kalshi_weather_edge.py` at 94KB
while newer 178KB versions live there); GitHub surface (workflows, branches, open
PRs, artifacts, caches, secret NAMES only — never values); infrastructure (Render
service `kalshi-dashboard` is actually served from the recycled
`polymarket-monitor2.onrender.com`; cron schedules; email alerts).

OUT: Polymarket copy monitor, Momentum Core.

## Division of labour

Duplicate only where independence buys something; single-owner everything mechanical.

| Work | Owner |
|---|---|
| Census / file catalogue | **Codex alone** (mechanical) |
| "What is live" spec | **BOTH, independently — then diff** |
| Headline numbers | **BOTH, independently — then diff** |
| Constant archaeology (why is this value this value) | **Codex alone** |
| `scripts/verify.py` | **Claude alone** (needs live API creds) |
| Claim reproduction (re-running research) | **Claude alone** (needs execution) |
| Doc restructure | **Claude alone** (single writer) |

## Sessions and deliverables

### Session 1 — independent derivation

**Codex → `docs/audit/codex/`**
- `INVENTORY.md` — every file in scope. Columns: path, size, last modified, last
  commit touching it, **reachable from the live path (yes/no, by import graph — not
  by judgement)**, tier A-E, status (`unaudited` initially).
- `LIVE_SPEC.md` — derived from code alone: every parameter the live trader
  actually uses, its value, and every gate in the entry path in order.
- `CONSTANTS.md` — for each live constant: value, the commit/comment/CLAUDE.md row
  that justifies it, the date, and a verdict — does the stated evidence actually
  support this value, is it stale, or is there none?

**Claude → `docs/audit/claude/`**
- `LIVE_SPEC.md` — same target, derived independently.
- `METRICS.md` — every headline number (win rate, break-even, margin, P&L, capture,
  EXTRA, $/trade, fill quality) re-derived from raw API + archive, cross-checked
  across all four sources: dashboard, `kstat`, `reconcile.py`, `backtest.py`.
- `scripts/verify.py` — re-derives every headline number from raw sources and
  **fails loudly on any disagreement**. This is the artifact that makes trust
  checkable rather than asserted.

### Session 2 — cross-check, then depth

Diff the two `LIVE_SPEC.md` files and the two number sets FIRST. **Every
disagreement is a bug in one of the agents** and is the highest-value output of the
whole audit. Resolve those before anything else.

Then: Codex audits Tier C claims (does the script still exist, does the stated
evidence support the live decision it justifies). Claude reproduces the claims that
currently gate a live decision.

### Session 3 — consolidation

One decision document: proposed moves, proposed deletions, proposed fixes, each with
evidence and a risk rating, for line-by-line approval. CLAUDE.md restructured into
**OPERATING** (what is live — short, Chris reads this), **EVIDENCE** (why — long),
**GRAVEYARD** (refuted, so nothing is re-tried). `verify.py` green.

**Nothing is executed in any session.**

## Pruning policy

**Move, do not delete.** Inert files go to `archive/`. Fully reversible. The
confusion comes from things being mixed together, not from things existing, and
deleting the one script that reproduces a claim costs a week. Byte-identical
duplicates are the single exception.

## Completeness — how we know nothing was skipped

The census is **generated**, never recalled: filesystem walk + `git ls-files` +
GitHub API + workflow list. Every row carries a status. A final gate asserts **zero
rows remain `unaudited`**. Skipping something becomes impossible to hide, because
the checklist is complete by construction rather than by diligence.

## Freeze declaration

In force from 2026-08-24 until Chris lifts it. The bot **keeps trading** — it earns
and keeps collecting gate-log/depth data and Silver's trade clock. Nothing about it
changes. Side benefit: this is the first clean multi-week measurement window in over
a month, which several open questions need anyway.
