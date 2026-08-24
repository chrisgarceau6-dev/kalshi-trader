# Session 2 — cross-check of the two independent Session 1 derivations

Claude `aedb471d` vs Codex `c0ba3c58`, diffed 2026-08-24. Per the charter, every
disagreement is a bug in one of the agents and is resolved here before any depth work.

Codex's deliverables are themselves in scope, so its claims are re-run rather than
read. Two do not hold; both are recorded below with the corrected evidence.

**Bottom line: we agree on every parameter and every gate.** There is not one
disagreement about what the trader does. The four findings below are about *evidence*
— which is the failure mode this project actually has.

---

## 1. Agreement on the live spec — complete

Independently derived, matching exactly: the six live series and seven shadow series;
`v5.17`; `$25` flat; band `[90,93]` effective with YES declared to 88; `150-600s`;
priors `75×2`; the `≤91c → 3rd prior ≥80c` gate; `LISTING_QUOTE_TOLERANCE=3` giving a
YES `85-96` / NO `87-96` prefilter; depth 39 first-attempt vs 60 on top-ups;
`max_conc=2` counting resting orders separately; `STOP_BALANCE=650`;
`max(300, bet×4)=$300`; `CONSEC_LOSS_LIMIT=9`; edge breaker 50/0.84/7200;
`BLACKOUT_HOURS=set()`; the `-secs_left` then MD5 slot sort; TTL 4 / reconcile 8 /
wait 3 / attempts 3 / min top-up $5; the 900s×15s daemon; 10-consecutive / 0.34-share
failure policy; and the longshot path being dead code.

Both flagged the same headline defect, both noted `backtest.py` ignores
`LOW_BAND_MIN_CENTS`, both noted the stale top-up depth, both catalogued substantially
the same doc drift.

Two independent derivations converging on every value is the strongest available
evidence that the spec is right.

---

## 2. FINDING D1 — Codex's production-log evidence for the headline bug is invalid

`docs/audit/codex/LIVE_SPEC.md` cites three runs as reproducing the defect:

> run `32742473985`: SOL fresh YES `88¢` is logged as having crashed below `90¢`;
> run `32744023345`: ETH fresh YES `89¢` … run `32739329589`: DOGE …

All three predate the v5.17 merge.

```
gh run list --workflow late_certainty.yml --limit 60 --json databaseId,headSha,createdAt
git log -1 --format='%H %ad' --date=iso-strict 584d0a85
```

| run | headSha | created | v5.17? |
|---|---|---|---|
| 32739329589 | `aadc3e6af` | 14:32:27Z | **no** |
| 32742473985 | `66e664ded` | 15:03:00Z | **no** |
| 32744023345 | `66e664ded` | 15:18:10Z | **no** |
| — | `584d0a85` merged | **16:22:31Z** | — |

The tell is in the quoted text itself: those logs read `crashed to 88.0000c (< 90c)`.
That message interpolates `_band_min(side)`. Printing **90** proves `_band_min("yes")`
returned 90 — i.e. `LOW_BAND_MIN_CENTS` did not yet exist. **They document v5.16
behaving correctly, not v5.17 being broken.**

### Corrected evidence

Seven runs were created after the merge. On deployed code the same message reads
`< 88c`, confirming the fresh-quote gate *does* honour the asymmetric floor, and the
rejection moves to the last look:

```
for r in 32760434238 32758954186 32757489283 32756022508 32754550981 32753071584 32751618426; do
  gh run view $r --log | grep -oE "last look: best yes offer 8[89]\.[0-9]+c is outside \[90,93\]c.*"
done
```
```
32760434238  last look: best yes offer 89.0000c is outside [90,93]c ... quote still said 90.0000c
32758954186  last look: best yes offer 89.0000c is outside [90,93]c ... quote still said 90.9000c
32758954186  last look: best yes offer 89.0000c is outside [90,93]c ... quote still said 90.0000c
```

Same conclusion, sound evidence. **This is why the parallel-audit design is worth the
cost:** the conclusion was right, the proof was not, and a single auditor would have
shipped the bad citation into Session 3.

It also narrows the defect usefully — §3.

---

## 3. FINDING D2 — one of the four hardcoded-90 sites blocks; the other three degrade

Codex lists four sites and treats them as one defect. I claimed gate 13 alone blocks.
Both descriptions are incomplete, and the difference decides the size of the fix.

Tested by patching **only** the last-look comparison in a scratchpad copy — the live
file is untouched — and running the real `try_trade` with a stubbed API through to a
simulated fill:

```
python3 docs/audit/claude/probe_fix.py
```

| ask | order placed | position | contracts | `outside_safe_zone` | top-ups |
|---|---|---|---|---|---|
| 88c | **yes** | taken | 27 | **True** | **stopped** |
| 89c | **yes** | taken | 27 | **True** | **stopped** |
| 90-93c | yes | taken | 26-27 | False | normal |

**Resolution: the one-line change at l.1320 is sufficient to make 88-89c trade, and
insufficient to make it trade correctly.**

| # | site | line | effect if left alone |
|---|---|---|---|
| 1 | last look | 1320 | **blocks outright** — the only gate that prevents the trade |
| 2 | top-up fresh-ask | 1367 | top-up stops; order fills at ~1 attempt instead of up to 3 |
| 3 | top-up book | 1384 | same |
| 4 | fill classification | 1511 | every valid 88-89c fill is flagged `outside_safe_zone`, and the `break` at l.1536 ends top-ups |

Site 4 is the one that matters beyond sizing: it writes a false `outside_safe_zone`
flag into state on every intended entry, which is the field any future analysis of
crash fills will read. Codex is right that a complete fix touches four sites; I was
right about the blocking mechanism. Neither spec said both.

---

## 4. FINDING D3 — the census silently drops 3,538 files

`docs/audit/codex/INVENTORY.md` documents its exclusions (`.git/`, `venv/`, bytecode,
`hunt_logs/`, the nested Ruflo cache, Polymarket-named root files) and Codex reports
"Scoped filesystem walk exactly matches all 260 inventory rows."

```
find . -path './.git' -prune -o -path './venv' -prune -o -path './docs/audit/claude' -prune -o -type f -print | wc -l
ls _tmp_*.csv | wc -l
grep -c '_tmp_' docs/audit/codex/INVENTORY.md
```
→ `4285` files walked, `3538` `_tmp_*.csv` in the repo root, **`0`** of them in the
inventory, and no mention of `_tmp` anywhere in the file.

They are untracked (`git ls-files | grep -c '^_tmp_'` → `0`) and total **253 MB of the
repo's 612 MB**. Excluding them is almost certainly correct. Not saying so is not:
the charter requires that "anything you cannot classify gets `UNKNOWN` and is
surfaced. Never silently bin something," and rests completeness on the census being
"complete by construction rather than by diligence."

260 rows is a curated list, not a census. The zero-unaudited gate in Session 3 will
pass over a denominator that omits 93% of the files in scope.

**Not a trading defect.** It is the one place where the audit's own completeness
guarantee does not hold, and it is cheap to fix: one documented exclusion line, or one
`UNKNOWN` row covering the glob.

---

## 5. FINDING D4 — Codex's C1 figure reproduces, but measures the wrong C1

`CONSTANTS.md` reports C1 removal at `-$106`, CI `[-$321,+$91]`, against CLAUDE.md's
older `+$260`, and marks it STALE/UNRESOLVED.

```
python3 scripts/backtest.py --compare c1=0
```
→ `delta -106, 98.75% CI [-321, +91], P(better)=0.117` — **reproduces exactly.**

But `backtest.qualifies` applies C1 to the YES side only, while the trader
(l.1246) has no side test and quarantines NO as well. Codex noted the integer
truncation and missed the side asymmetry; I noted the asymmetry and had not priced it.
Measured both ways, live series, 0.105c slip:

| C1 as applied | n | removing C1 | 98.75% CI | P(better) |
|---|---|---|---|---|
| YES-only (`backtest.qualifies`) | 8900 | −$96 | [−310, +95] | 0.156 |
| **both sides, `int()`-truncated (the trader)** | 8837 | **−$52** | [−310, +191] | 0.342 |

The verdict does not change — nothing is established either way — but the number
CLAUDE.md will carry should be the live rule's, and the harness comment claiming it
"matches the live trader" is false.

---

## 6. Findings each agent has alone

Not disagreements — different lanes, per the charter's division of labour. Listed so
Session 3 pulls from both.

**Codex only** (constant archaeology and the order layer, its lane):
order body semantics verified against current Kalshi V2 docs — `taker_at_cross`,
`cancel_order_on_pause`, 4-decimal fixed price, YES=`bid` / NO=`ask` at `1−price`;
the 20s signed-HTTP timeout; the 3-page ambiguous-POST lookup; the candle request
ending 20s before now; per-constant verdicts including **`STOP_BALANCE=650` marked
STALE** (set proportionally to a $100 stake, never revalidated at $25) and
**NONE-grade evidence** for the edge breaker's 50/0.84, `ORDER_TTL_SECONDS`,
`ORDER_RECONCILE_SECONDS`, `ORDER_MIN_TOPUP_DOLLARS` and the 500-position state cap.

**Claude only** (metrics and claim reproduction, my lane):
lifetime P&L is not reproducible from any source, and the −$487 in circulation matches
none of the three defensible scopes; the ET-vs-UTC day key moves 20.7% of trades and
$222 on 2026-08-19; `backtest.py`'s headline includes three shadow series
(+0.280 vs +0.299/tr); the v5.17 point estimates reproduce exactly while its CI lower
bound does not (`[+68,+826]` claimed, `[−6,+859]` measured across six seeds); the
+0.105c fill constant is stale and now sign-flipped; EXTRA's worst sub-bucket is
trades the model would itself take; live WR is not established as below the model's
(z = −1.81); the last-look gate rejects 63% of in-band YES quotes, a filter absent
from CLAUDE.md's funnel; and the checked-clean set (fee units, state-vs-API exact on
500 positions, archive contiguity, cap enforcement, deployed bet size).

---

## 7. What Session 3 should carry

| # | Item | Owner of the fix | Risk |
|---|---|---|---|
| 1 | Last-look uses `MIN_ASK_CENTS`, not `_band_min(side)` — 88-89c cannot trade | **Chris's call.** 1 line to unblock, 4 to be correct | live, ~+$5/day forgone |
| 2 | Sites 2-4 mislabel and under-size any 88-89c fill | same change set | corrupts `outside_safe_zone` |
| 3 | `backtest.py` ignores `LOW_BAND_MIN_CENTS` and scores 3 shadow series | harness, not live path | every v5.17 number |
| 4 | `backtest.qualifies` C1 is YES-only; trader is both sides | harness | −$44 of measured delta |
| 5 | Top-up depth still 60; live gate is 39 | live path | smaller fills |
| 6 | Day key differs across four tools | tooling | $222 on one day |
| 7 | Census omits 3,538 files undocumented | `docs/audit/codex/` | completeness gate |
| 8 | `STOP_BALANCE=650` stale at $25; breaker thresholds have no evidence | Chris's call | unquantified |

Item 1 needs a decision before anything else: while it stands, the v5.17
pre-registration cannot accumulate its n=200, so its clock has not started.
