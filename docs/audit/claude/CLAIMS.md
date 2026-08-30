# Session 2 depth — reproducing the claims that gate live decisions

Claude's Session 2 lane per the charter. Each claim below currently gates a live
decision or is quoted as the basis for one. Every figure carries a command.

Verdicts: **HOLDS** — reproduces and survives being taken apart. **HOLDS, RESTATED** —
conclusion survives but the written number is wrong. **FAILS** — does not reproduce.

| # | Claim | Verdict |
|---|---|---|
| 1 | Fill quality is +0.105c adverse | **FAILS** — measured +0.227c, t=+6.6 |
| 2 | Low-ask gate: p3≥80 at ≤91c | **HOLDS** — P=0.996 |
| 3 | Pre-2026-08-22 archive rounding is a minor effect | **FAILS** — 40.4% of selections disagree |
| 4 | C1 quarantine is worth ~$260 | **FAILS** — −$52, nothing established (`DIFF.md` §5) |
| 5 | Crash fills silently upsize the position | **FAILS** — compared against a superseded bet size; mechanism is structurally impossible |

---

## 1. FAILS — fill quality is 2.2x the documented figure

CLAUDE.md l.175 and §4: *"Execution gap = +0.105¢ (measured 2026-08-17)… Re-measure
after NO accumulates settlements; we have **no NO-side fill data at all**."* That
constant is what every `--slip 0.105` run in the file is built on.

It is now measurable, on both sides, from the `book_at_entry` / `book_age_ms` fields
the trader began recording on 2026-08-21. This is the comparison the trader's own
comment (l.1470) says is the valid one — against **the book read ~128ms before the
order**, over distributions — not against a 1-min candle, which is stale 47% of the
time and produces a +0.85c regression artifact.

```
python3 scripts/verify.py --check slippage
```

| side | n | mean | median | sd | p10 | p90 | share paying above book |
|---|---|---|---|---|---|---|---|
| YES | 269 | **+0.253c** | +0.100 | 0.394 | −0.00 | +0.78 | 68% |
| NO | 231 | **+0.196c** | +0.085 | 0.430 | −0.03 | +0.71 | 66% |
| **all** | **500** | **+0.227c** | +0.097 | 0.412 | −0.00 | +0.75 | 67% |

Positive means paying **more** than the book's best offer — adverse. Worked example:
a YES entry whose book best offer was 90.0c filled at 90.543c.

* **+0.227c against a documented +0.105c**, SE 0.018, **t = +6.6**. Not a drift; the
  constant is wrong by more than double.
* **YES − NO = +0.058c, t = +1.55 — no difference established.** The "no NO-side fill
  data at all" open question is now answered, and the answer is that NO fills as well
  as YES. The main stated risk of v5.16 did not materialise.
* Only 5.8% of fills are worse than the book by more than 1c, so this is a shifted
  centre, not a fat tail.

### What the wrong constant costs

```
python3 scripts/backtest.py --slip 0.227      # vs the 0.105 everything is quoted at
```

| assumption | total | $/trade | $/day | vs quoted fills |
|---|---|---|---|---|
| quoted fills | +$2,898 | +0.326 | +$39 | — |
| CLAUDE.md's 0.105c | +$2,657 | +0.299 | +$36 | −8.3% |
| **measured 0.227c** | **+$2,378** | **+0.267** | **+$32** | **−17.9%** |
| one full tick | +$628 | +0.071 | +$8 | −78.3% |

Every "at measured fill quality" figure in CLAUDE.md is optimistic by ~$279 over the
archive — about 10% of the stated edge. That includes the `max_conc` sweep, the
`MAX_ASK_CENTS=93` reasoning, the Silver lead, and the v5.17 pre-registration.

**This does not invert any decision I can find** — the ranking of `max_conc=2` and the
sign of the live edge both survive at 0.227c. It moves every magnitude.

Why the two tools disagree: `reconcile.py` reports fill quality as **−0.062c
favourable** over the same period. It compares the live fill against the *model's
candle ask*, which is a different quantity — it includes selection differences between
live and model entries, and inherits exactly the staleness the trader's comment warns
about. **The book comparison is the execution measurement; the reconcile figure is
not.** Neither is wrong, but only one answers "what does execution cost".

---

## 2. HOLDS — the low-ask gate is sound, and correctly scoped

Trader l.1237: *"Cross-tab (n=2035): 90-91c + prior2-only = -$0.08/trade (below
break-even). 90-91c + prior3>=80c = +$0.93/trade. 92-93c is +EV with prior2 alone."*

Live series, base gates applied, C1 as the harness applies it:

| band | p3 filter | n | $/tr @0c | $/tr @0.105c |
|---|---|---|---|---|
| 90-91c | p3 ≥ 80 (**kept**) | 5,725 | +0.427 | **+0.399** |
| 90-91c | p3 < 80 (**blocked**) | 4,892 | +0.025 | **−0.002** |
| 92-93c | p3 ≥ 80 | 7,254 | +0.132 | +0.105 |
| 92-93c | p3 < 80 | 4,757 | +0.341 | +0.315 |

Cluster bootstrap, `(p3≥80) − (p3<80)` at 0.105c slip:

| band | delta | 98.75% CI | P(p3≥80 better) |
|---|---|---|---|
| **90-91c** | **+$2,298** | **[+373, +4,161]** | **0.996** |
| 92-93c | −$734 | [−2,601, +1,143] | 0.180 |

**The gate is one of the few live filters whose CI excludes zero.** Blocking p3<80 at
90-91c removes a population that runs at break-even and keeps one at +$0.40/trade.

The written per-trade numbers are stated at a **$50 bet**, not the live $25:

| bet | 90-91c p3≥80 | 90-91c p3<80 |
|---|---|---|
| $25 (live) | +0.427 | +0.025 |
| $50 | **+0.854** | +0.050 |
| $75 | +1.281 | +0.074 |

`+0.854` at $50 is the claimed `+$0.93`. The claim is right, its units are stale, and
`n=2035` has since grown to 10,617.

### Lead, not a finding: the p3 signal inverts above 91c

At 92-93c the ordering reverses — p3<80 measures **+0.315** against p3≥80's **+0.105**.
The trader gates only at `≤91c`, so it is correctly not acting on this. The CI
includes zero (P=0.180), and Invariant 8 is explicit that post-hoc filter discovery
has a terrible record here. **Recorded as a lead requiring pre-registration, not a
proposal.**

---

## 3. FAILS — archive rounding is far larger than recorded

CLAUDE.md: *"An audit on 2026-08-22 probed 180 markets and found rounding changed
18.4% of selected identities"* and *"106 of 2,563 rows (4.1%) change band under
rounding."*

Those measure rows. The quantity that matters for every backtest claim is **which
trades the simulator picks**. Measured by running the two exact-cent days
(2026-08-22/23) through the live config twice — once with true prices, once with the
prices artificially rounded the way every earlier archived day stores them:

```
python3 scripts/verify.py --check rounding
```

| | trades | WR | total | $/trade |
|---|---|---|---|---|
| exact prices | 237 | 94.51% | +$149 | +0.629 |
| rounded prices | 269 | 94.80% | +$175 | +0.652 |
| **rounding effect** | **+32 (+13.5%)** | **+0.28pp** | **+$26** | **+0.023** |

Selected identities: 237 exact, 269 rounded, **128 of 317 disagree — 40.4%**, with 80
trades appearing only under rounding and 48 disappearing.

**Rounding biases every pre-Aug-22 backtest optimistic**, in all three of trade count,
win rate and per-trade edge. It inflates volume by 13.5% and $/trade by $0.023 — the
latter is about 8% of the current measured edge, in the same direction as the
slippage error in §1, so the two compound rather than cancel.

Caveat, stated plainly: this is **two days, n=237**. It establishes the direction and
the rough size, not a precise correction factor. Every day before 2026-08-22 carries
it, which is 72 of 74 archived days, and Kalshi's ~67-day retention means days before
~2026-06-18 can no longer be re-archived.

Affected, because each was derived pre-Aug-22: `MIN_ASK_CENTS=90`,
`PRIOR_MIN_CENTS=75`, `PRIOR_LOOKBACK=2`, the low-ask gate's original cross-tab,
edge-by-price, entry-timing, and every config sweep. §2 above re-derives the low-ask
gate on the full archive, so it carries the bias too — but its CI is wide enough
(+373 to +4,161) that a 13.5% volume effect does not threaten the sign.

---

## 5. FAILS — "a crash fill silently UPSIZES" compares against a superseded bet size

CLAUDE.md (z-gate row, Aug 29) records the Aug 29 crash fill and adds a separate
finding: *"it filled 37 contracts for $31.45 against a $25 flat bet — 26% over. A
cheaper fill buys more contracts, so a crash fill silently UPSIZES exactly when the
book is disorderly. Pre-existing, unrelated to the z-gate, and not currently
controlled."*

The arithmetic is right and the conclusion is wrong, for two independent reasons.

```
python3 docs/audit/claude/probe_fillsize.py
```

**(a) The bet was $35 that day, not $25.** #224 moved `FLAT_BET_DOLLARS` 25 → 35 on
Aug 28 — the day *before* the fill. Aug 29's median settlement cost is **$34.04**.
The flagged fill is **$31.45, a ratio of 0.92x — below the day's typical size**, not
26% above it. `contracts_for_risk(35, 0.87) = 40`; 37 filled at 85.00c is a normal
order that filled slightly short and slightly cheap.

**(b) The mechanism cannot do what the note says.** `contracts_for_risk`
(`late_certainty_trader.py:470`) sizes off **`limit_cents`**, not the expected fill
price — and `limit_cents = min(MAX_ASK_CENTS, entry_ask + LIMIT_BUFFER)` (l.1695) is
the *worst* price the order can pay. So a cheaper fill spends strictly **less** than
the bet; it cannot spend more. Above that, the top-up loop accumulates
`total_cost` and raises an **EXECUTION HALT** whenever cumulative principal exceeds
the budget (l.1874). Two independent guards, both holding.

**Confirmed empirically: nothing exceeds 2.4x its day's size, ever.**

| cost / day's median cost | n | share | P&L | WR | ROC |
|---|---|---|---|---|---|
| <0.6x (partial fills) | 35 | 1.7% | +$68.61 | 97.1% | +16.65% |
| 0.6–1.15x (normal) | 1,875 | 92.7% | +$779.25 | 93.9% | +1.10% |
| 1.15–1.6x | 56 | 2.8% | +$34.93 | 94.6% | +1.71% |
| 1.6–2.4x | 56 | 2.8% | −$6.27 | 91.1% | −0.20% |
| **>2.4x** | **0** | — | — | — | — |

**And the 1.6–2.4x cohort is not overshoot either — it is deploy days.**
All 56 fall on four dates, and each is bimodal because a sizing deploy lands mid-day:

| day | n ≥1.6x | day splits as | what happened |
|---|---|---|---|
| Aug 5 | 2 | 42 @ ~$35.82 + 4 @ ~$54.52 | sizing deploy mid-day |
| Aug 9 | 1 | 40 @ ~$45.57 + 1 @ ~$92.00 | single outlier |
| Aug 14 | 11 | 49 @ ~$45.57 + 12 @ ~$73.97 | $50 → $75 landed Aug 14, not Aug 15 |
| Aug 22 | 42 | 56 @ ~$23.95 + 42 @ ~$48.87 | #151, $50 → $25 |

Net across the whole cohort: **−$6.27 on 2.8% of trades.** There is no oversizing
problem to control.

### The trap, because it caught this analysis first

`FLAT_BET_DOLLARS` moved **six times** in this window — ~$36 → $45.6 → $73.5 → $48.8
→ $24 → $34 — and a deploy lands mid-day, so a single day spans two sizes. Scoring
fill size against a *remembered* bet constant manufactures a large fake "oversized"
cohort with a plausible-looking loss attached; a first pass at this produced a
confident **−$129.79** that was entirely an artifact of the wrong era map. The only
safe denominator is **the day's own median settlement cost**, cross-checked for
bimodality on deploy days. `probe_fillsize.py` does both.

The same defect is what put the claim in CLAUDE.md: the Aug 29 note was written
against $25 one day after the bet became $35. **When quoting a per-trade dollar
figure here, derive the bet size from the data, never from the file.**

---

## Standing after this session

| claim | status |
|---|---|
| Low-ask gate p3≥80 at ≤91c | **established** — the only live filter with a CI excluding zero |
| NO-side fill quality | **answered** — indistinguishable from YES (t=1.55); v5.16's main stated risk did not materialise |
| Fill quality constant | **replace 0.105 with 0.227**; re-quote every "measured fill quality" figure |
| Archive rounding | **direction and size established**; precise correction not recoverable pre-Aug-22 |
| C1 quarantine | not established either way (`DIFF.md` §5) |
| p3 inversion above 91c | **lead only** — needs pre-registration before anyone acts |
| Crash-fill upsizing | **refuted** — remove the claim from CLAUDE.md's z-gate row; two guards hold and >2.4x is empty |

Both new checks are wired into `scripts/verify.py` (`--check slippage rounding`) so
they re-derive on demand instead of ageing into prose.
