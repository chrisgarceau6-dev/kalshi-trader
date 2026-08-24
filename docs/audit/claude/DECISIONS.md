# Decision sheet — for line-by-line approval

Everything the audit found that implies a change, with evidence, a risk rating, and a
recommendation. **Nothing here has been executed.** The freeze holds until you say
otherwise, item by item.

Codex's Tier C output is still landing; items from it will be appended rather than
merged into these, so the numbering stays stable.

**How to use this:** each item has an ID. Reply with the IDs you approve
(`A1 yes, A2 no, B1 yes…`). Anything you don't name stays frozen.

Risk key — **R1** no trading effect · **R2** changes what is measured, not what is
traded · **R3** changes which trades are placed · **R4** changes risk exposure.

---

## Group A — live path. These change what the bot does.

### A1 · Fix the last-look band gate so 88-89c YES can execute — **R3**

**What:** four sites use `MIN_ASK_CENTS` where they should use `_band_min(side)`:
l.1320 (last look), l.1367 and l.1384 (top-up), l.1511 (fill classification).

**Why it matters:** l.1320 blocks the trade outright. The other three do not block —
they mislabel. Measured, not argued (`docs/audit/claude/probe_fix.py`): patching only
l.1320 lets 88-89c place an order, but the fill is then flagged `outside_safe_zone`
and the `break` at l.1536 stops top-ups, so it fills once instead of up to three
times. **Site 4 writes a false flag into state on every intended entry** — the field
any future crash-fill analysis will read.

**Value, at the measured 0.227c slippage rather than the documented 0.105c:**

| slippage | baseline | v5.17 | delta | 98.75% CI | P(better) |
|---|---|---|---|---|---|
| 0.105c (as pre-registered) | +$2,657 | +$3,101 | +$443 | [−6, +859] | 0.986 |
| **0.227c (measured)** | **+$2,378** | **+$2,812** | **+$434** | **[−15, +848]** | **0.984** |
| 0.35c (stress) | +$2,098 | +$2,522 | +$424 | [−24, +837] | 0.984 |

**~$5.86/day. The effect barely moves across slippage** — it is not a fill-quality
artifact, which is the main way this class of finding usually dies.

**The honest framing:** this is not recovering lost profit. It is *starting* the
experiment that was supposed to start on 2026-08-24. The CI touches zero at every
slippage level, so the backtest says "probably better", not "better".

**Recommend: approve all four sites, not just l.1320.** A partial fix trades the band
while corrupting the record of how it filled — the worst of both.

### A2 · Decide what happens to the v5.17 pre-registration — **R1, but it governs A1**

The registered rule was: *200 88-89c-YES trades, revert if subset WR < 88.5%, do not
read early.* Zero such trades have ever been placed, so **the clock has not started**
and the rule has never been live.

Three options, and this is a judgment call I should not make for you:

1. **Restart the clock on the original terms** when A1 ships. Cleanest; honours the
   pre-registration as written.
2. **Re-register at the corrected slippage.** The original quoted +$0.39/tr for the
   subset at 0.105c; the honest figure is lower.
3. **Don't ship A1.** The CI touches zero; this is a legitimate call, not a failure.

**Recommend option 1.** The point of pre-registration is that you don't get to revise
it after seeing anything — and nothing has been seen.

### A3 · Top-up depth uses the retired constant — **R3**

l.1388 requires `MIN_BOOK_DEPTH` = 60. First attempts moved to `min_book_depth()` = 39
at the current $25 bet (PR #166's own reasoning: a fixed 60 tightened every time the
bet was cut, which is backwards). Same order, two thresholds.

**Effect:** top-ups are refused on books that would fill them, so orders fill smaller
than intended. Both auditors found this independently.

**Recommend: approve.** It restores the intent of a change already made and reviewed.

### A4 · `STOP_BALANCE = 650` is stale — **R4, and I cannot price it**

Set on 2026-08-14 proportionally to a **$100** stake. The stake is now $25. Codex
rates it STALE with no later revalidation; I found no evidence tying 650 to current
sizing.

**No recommendation.** This is a survival parameter and the right value depends on how
much of the account you are willing to risk on the experiment continuing — which is
yours, not derivable from the archive. Flagging that it is unexamined, not that it is
wrong.

---

## Group B — measurement. These change what the numbers say, not what is traded.

### B1 · `backtest.py` cannot express the live strategy — **R2**

Three separate defects in the harness every claim is checked against:

| # | defect | effect |
|---|---|---|
| a | ignores `LOW_BAND_MIN_CENTS` — applies one symmetric band | cannot model v5.17 at all, while labelling output `v5.17` |
| b | scores all archived series, including `KXGOLD15M`/`KXSILVER15M`/`KXWTI15M` | +0.280/tr shipped vs **+0.299/tr** on live series |
| c | `qualifies()` applies C1 to YES only; the trader has no side test | −$44 of measured C1 delta, and its comment claims it matches |

**Recommend: approve all three.** This is the tool that decides every future claim; it
is currently unable to represent the running strategy. Not a trading change.

### B2 · Replace the fill-quality constant, 0.105c → 0.227c — **R2**

Measured against `book_at_entry` on n=500: **+0.227c, SE 0.018, t = +6.6** — 2.2x the
documented figure. YES +0.253c, NO +0.196c, difference t=1.55, so **NO fills as well
as YES** and CLAUDE.md's "no NO-side fill data at all" is resolved.

Every `--slip 0.105` figure in the file is optimistic by ~10% of the stated edge.

**I re-ran every config sweep at both levels. No ranking inverts** — `max_conc`,
`max_ask`, `min_ask`, `prior_min`, `lookback`, `min_secs`, `max_secs` and `yes_only`
all keep the same ordering. So this corrects magnitudes, not decisions.

**Recommend: approve.** Also update `DOCUMENTED_SLIP_CENTS` in `scripts/verify.py`, so
the check tracks the documented number rather than a hardcoded one.

### B3 · Four tools, three different day keys — **R2**

`reconcile.py` and the archive key on the **UTC** close day; `kstat.py` and the
dashboard key on the **ET** day. 20.7% of trades land on a different date; 2026-08-19
reads **+$60.29 or −$161.68** depending on which you ask.

**Recommend: standardise on UTC close day**, because the archive is keyed that way and
the backtest is the comparison target. Secondary: `kstat.py` hardcodes `UTC−4`, which
is EDT and will be silently wrong from 2026-11-01.

### B4 · `kstat` counts a win as `pnl > 0` — **R2**

Everything else uses `market_result == side`. One row in 1,948 disagrees
(`KXBNB15M-26AUG140930-30` settled a win at exactly $0.00). Rare, not theoretical.

**Recommend: approve** — read the outcome rather than inferring it from P&L.

### B5 · The gate log records a threshold the trader does not use — **R2**

`shadow_gate_inputs` writes `min_depth=60`; the live gate needs 39. Any replay that
reconstructs "would depth have blocked this?" from the logged field over-counts
blocks. This matters because the Aug 23 depth-gate conclusion — *"`MIN_BOOK_DEPTH=60`
is a near-perfect availability detector, do not loosen it"* — is an argument about a
threshold the trader stopped using when the bet was cut.

**Recommend: approve**, and re-derive that conclusion at 39 before relying on it.

---

## Group C — record. No trading effect.

### C1 · Retire "lifetime P&L" as a quotable number — **R1**

No source on this machine can produce one. Settlements retain ~30 days; state caps at
500 positions; `stats` resets on every version bump (it currently reads **1 trade**).
The −$487 in circulation matches none of the three defensible scopes:

| scope | n | P&L |
|---|---|---|
| live series | 1,946 | **−$320.91** |
| + retired 15M (what the dashboard counts) | 2,253 | **−$565.96** |
| + non-15M (KXMLBTOTAL −$863.75 on 4 trades) | 2,258 | **−$1,445.00** |

**Recommend:** quote a scope and a window, never a bare "lifetime".

### C2 · Correct the stale prose — **R1**

Module docstring says v5.12, $75, YES-only, ET13 excluded, 5 consecutive losses,
$600 daily limit. Workflow banner says v5.16, $75, WTI live. All wrong. Full list in
`LIVE_SPEC.md` §7.

### C3 · Record that archive rounding is larger than believed — **R1**

Not 18.4% of identities — **40.4% of selected trades** (128 of 317), volume +13.5%,
$/trade +0.023, always optimistic, compounding with B2 in the same direction. 72 of 74
archived days carry it and days before ~2026-06-18 can no longer be re-archived.

### C4 · Fix the census gap — **R1, `docs/audit/codex/`**

`INVENTORY.md` claims the walk "exactly matches all 260 rows"; the walk returns 4,285.
3,538 `_tmp_*.csv` (253MB of 612MB) appear nowhere, undocumented. Sent to Codex.

### C5 · Restructure CLAUDE.md into OPERATING / EVIDENCE / GRAVEYARD — **R1**

Charter deliverable, mine as single writer. Blocked until A1-A4 are decided, since
OPERATING has to state what is actually live.

---

## What stays frozen regardless

Recorded so nobody mistakes silence for approval:

- **The p3 inversion above 91c.** p3<80 measures +0.315 vs +0.105 at 92-93c. The
  trader correctly does not gate there. CI includes zero (P=0.180) and Invariant 8 is
  explicit about post-hoc filter discovery. **Lead only; needs pre-registration.**
- **`max_conc`, `max_ask`, `min_ask`, `min_secs`.** The sweep optimum differs from the
  live value on all four at both slippage levels, and every one has a CI including
  zero. Unchanged.
- **Edge breaker 50/0.84, `ORDER_TTL_SECONDS`, `ORDER_RECONCILE_SECONDS`,
  `ORDER_MIN_TOPUP_DOLLARS`, the 500-position state cap.** Codex rates these NONE for
  value-specific evidence. Unexamined is not the same as wrong; none is changed here.

---

## Getting `verify.py` green

Currently **2 FAIL, 6 WARN**. It cannot go green while the findings stand, which is
the point — it is measuring real disagreements, not style.

| check | clears when |
|---|---|
| `deadband` FAIL | **A1** ships |
| `slippage` FAIL | **B2** ships (constant updated to match measurement) |
| `daykey` WARN | **B3** |
| `harness` WARN | **B1b** |
| `gates` WARN | **B1c + A3 + B5** |
| `series` WARN | dashboard filter aligned to `SERIES_LIST` |
| `rounding` WARN | never — it is a permanent property of pre-Aug-22 data |
| `win` WARN | **B4** |

`rounding` should stay a WARN forever. The others should all clear.
