# METRICS — every headline number, its definition, its source, and its cross-check

Derived 2026-08-24 at `ee5cadd4`. Raw truth is the Kalshi API; nothing here is taken
from CLAUDE.md, a dashboard screenshot, or a remembered figure.

Run everything from `~/pm`. `scripts/verify.py` re-derives all of it and fails loudly
on any disagreement:

```
python3 scripts/verify.py                    # green check or a named disagreement
python3 scripts/verify.py --since 2026-08-23 # one window
python3 scripts/verify.py --json             # machine-readable
```

---

## 1. The four sources, and what each one actually is

| Source | What it reads | Day key | "win" means | Series filter |
|---|---|---|---|---|
| `kalshi_dashboard.py` | `/portfolio/settlements` | `settled_time`, browser-local | `revenue > 0.01` | ticker **ends with `15M`** |
| `scripts/kstat.py` | `live-state` run artifact | `opened_at`, **hardcoded UTC−4** | **`pnl > 0`** | none (whatever is in state) |
| `scripts/reconcile.py` | `/portfolio/settlements` + `/portfolio/fills` | close day in **UTC** (`_close_day`) | `market_result == side` | `SERIES_LIST` by AST |
| `scripts/backtest.py` | `data/candles/*.csv.gz` | archive filename = **UTC** day | archive `won` column | **none — all archived series** |

Four tools, three different day keys, two different win definitions, three different
series filters. Sections 2-8 quantify what each divergence is worth.

---

## 2. Fee and revenue units — CHECKED, CORRECT

The charter's `volume_fp` bug made every unmarked field suspect. Settlement records mix
conventions in one object, so this had to be pinned:

```
python3 -c "
import json,sys;print(json.dumps(json.load(open(sys.argv[1]))[0],indent=1))" <settlements.json>
```
```json
{"ticker":"KXETH15M-26AUG241345-45","revenue":2600,"fee_cost":"0.132500",
 "yes_count_fp":"26.00","yes_total_cost_dollars":"23.946000","market_result":"yes"}
```

* `revenue` = **cents** (26 contracts × $1 = 2600). Both dashboard and reconcile divide by 100. ✓
* `fee_cost` = **dollars**. Kalshi's schedule is `ceil(0.07·C·P·(1−P))` cents:
  `0.07 × 26 × 0.921 × 0.079 = $0.1324` against the reported `$0.1325`. ✓
* `yes_total_cost_dollars` = dollars: `26 × 0.921 = 23.946`. ✓

**No units bug here.** Recorded as a checked-and-clean node, not skipped.

Realised fee, measured rather than modelled:

```
python3 -c "
import json,sys;S=json.load(open(sys.argv[1]))
f=sum(float(s['fee_cost']) for s in S); c=sum(max(float(s['yes_count_fp']),float(s['no_count_fp'])) for s in S)
print(f'{100*f/c:.4f}c per contract')" <settlements.json>
```
→ **0.528c/contract** over Aug 15-24, live series. This is the term that belongs
*inside* break-even (charter bug #2); it is 0.53pp of win rate.

---

## 3. The definitions, stated once

Let `R` be a set of settled positions. For each: `cost` (dollars paid), `ct`
(contracts), `fee` (dollars), `rev` (`$1 × ct` if won else `0`).

| Metric | Definition |
|---|---|
| **P&L** | `Σ(rev − cost − fee)` |
| **Win rate (trade-weighted)** | `#{won} / #R` |
| **Win rate ($-weighted)** | `Σcost(winners) / Σcost(all)` |
| **Break-even** | `(Σcost + Σfee) / Σct`, with losers' `ct` imputed at the winner-implied average price |
| **Margin** | `$-weighted WR − break-even`, in pp |
| **$/trade** | `P&L / #R` |
| **Avg fill** | `100 · Σcost / Σct` cents |
| **Capture** | `#(model ∩ live) / #model` |
| **EXTRA** | live took it, the model at live config would not |

`kstat.py::margin` and the dashboard's JS `margin()` implement the same formula.
`verify.py` asserts they agree on identical rows; they do, to <0.01pp.

**A trade-weighted win rate against a flat 92% is the wrong comparison** and break-even
must carry fees. Both are already fixed in kstat and the dashboard.

---

## 4. Headline numbers, per day

Canonical source: `/portfolio/settlements`, live series only, bucketed by **UTC close
day**. Reproduce with `python3 scripts/verify.py --table`.

| close day (UTC) | n | WR | $-wtd WR | break-even | **margin** | P&L | $/tr | avg fill | avg cost |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-15 | 35 | 97.14% | 96.89% | 92.09% | **+4.80pp** | +124.01 | +3.54 | 91.63c | $68.04 |
| 2026-08-16 | 23 | 91.30% | 91.31% | 91.96% | −0.65pp | −11.95 | −0.52 | 91.46c | $73.45 |
| 2026-08-17 | 26 | 92.31% | 92.35% | 92.26% | +0.09pp | +1.81 | +0.07 | 91.73c | $73.54 |
| 2026-08-18 | 113 | 90.27% | 90.35% | 91.39% | −1.04pp | −92.64 | −0.82 | 90.84c | $71.62 |
| 2026-08-19 | 60 | 88.33% | 87.75% | 92.15% | **−4.40pp** | −161.68 | −2.69 | 91.62c | $56.07 |
| 2026-08-20 | 93 | 94.62% | 94.55% | 92.04% | +2.51pp | +122.65 | +1.32 | 91.50c | $48.03 |
| 2026-08-21 | 89 | 87.64% | 87.47% | 92.21% | **−4.74pp** | −221.26 | −2.49 | 91.70c | $48.11 |
| 2026-08-22 | 93 | 89.25% | 88.44% | 92.13% | −3.69pp | −144.52 | −1.55 | 91.60c | $38.62 |
| 2026-08-23 | 135 | 94.81% | 94.84% | 92.07% | +2.77pp | +98.12 | +0.73 | 91.54c | $24.01 |
| 2026-08-24 (partial) | 95 | 96.84% | 96.86% | 92.15% | +4.71pp | +117.19 | +1.23 | 91.61c | $23.98 |
| **Aug 15-24** | **762** | **92.26%** | **91.51%** | **91.95%** | **−0.44pp** | **−168.27** | **−0.22** | 91.42c | — |

`avg cost` is the only reliable record of what was actually bet: **$75 through Aug 18,
$50 Aug 19-21, transition Aug 22, $25 from Aug 23.** Any $/trade figure spanning
2026-08-22 is dollar-weighted across two bet sizes and is not a per-trade edge.

### The clean window
2026-08-23 → 2026-08-24 is the first period at a single bet size:

| | n | WR | P&L | $/tr | avg cost |
|---|---|---|---|---|---|
| all | 230 | 95.65% | **+215.31** | +0.936 | $24.00 |
| YES | 118 | 96.61% | +136.39 | +1.156 | |
| NO | 112 | 94.64% | +78.92 | +0.705 | |

YES−NO win-rate difference **+1.97pp, z = 0.73 — not significant**. The v5.16 decision
to re-enable NO is not contradicted by live data.

### Per series, three eras

| series | Jul27-Aug24 $/tr | Aug15+ $/tr | Aug22+ $/tr |
|---|---|---|---|
| KXXRP15M | +0.824 | +1.348 | +1.542 |
| KXBNB15M | +0.496 | +1.171 | +0.414 |
| KXDOGE15M | +0.100 | −1.197 | −0.251 |
| KXETH15M | −0.676 | −0.254 | +0.612 |
| KXBTC15M | −0.437 | −0.594 | −0.467 |
| KXSOL15M | −1.253 | −0.938 | −0.722 |

Only SOL and BTC keep their sign across all three windows. The others flip. Do not
act on a per-series ranking from any single window.

---

## 5. FINDING M1 — "lifetime P&L" is not reproducible, and the number in circulation is wrong

`/portfolio/settlements` retains **~30 days**. Full pagination exhausts at 12 pages:

```
python3 -c "
import json,sys;S=json.load(open(sys.argv[1]))
d=sorted({s['settled_time'][:10] for s in S});print(len(S),'settlements',d[0],'->',d[-1])" <settlements_all.json>
```
→ `2258 settlements 2026-07-25 -> 2026-08-24`

The state artifact is capped at `MAX_POSITIONS_STATE = 500`, and `stats` is reset on
every `STRATEGY_VERSION` bump (it currently reads **1 trade**). **No source on this
machine can produce a lifetime figure.** What the API window does contain:

| scope | n | P&L |
|---|---|---|
| live series (6) | 1946 | **−320.91** |
| + retired 15M (HYPE, NEAR, WTI) — what the dashboard counts | 2253 | **−565.96** |
| + non-15M (KXMLBTOTAL ×4, KXMVESPORTS ×1) | 2258 | **−1445.00** |

**`KXMLBTOTAL` alone is −$863.75 on 4 trades (−$215.94/trade).** It is not the
late-certainty strategy and is correctly outside the dashboard's strategy stats, but it
is by far the largest single item in the account and belongs in any account-level
number.

The "−$487 lifetime" figure in circulation matches none of these. **Treat it as
retired.** The reproducible statements are the three rows above, each with its scope
named.

The window also spans a regime break that makes pooling meaningless:

| week (UTC close day) | n | WR | P&L |
|---|---|---|---|
| Jul 27-28 | 145 | **34.48%** | −501.67 |
| Jul 29-31 | 114 | 57.89% | −113.63 |
| Aug 1-7 | 555 | 96.04% | +217.31 |
| Aug 8-14 | 354 | 93.79% | +153.34 |
| Aug 15-21 | 455 | 91.43% | −147.05 |
| Aug 22-24 | 323 | 93.81% | +70.79 |

---

## 6. FINDING M2 — the day key differs between tools, and it is worth $222 on a bad day

`reconcile.py::_close_day` adds 4h to the ticker's ET timestamp, i.e. it keys on the
**UTC** day of close (matching the archive filenames). `kstat.py` buckets by
`opened_at` at a hardcoded UTC−4, i.e. the **ET** day. The dashboard buckets by
`settled_time` in browser-local time.

```
python3 scripts/verify.py --check daykey
```

**20.7% of all live trades (403 of 1946) land on a different calendar day** depending
on which definition is used — every close between 20:00 and 23:59 ET.

| label | ET-day n | ET-day P&L | UTC-day n | UTC-day P&L | Δ |
|---|---|---|---|---|---|
| 2026-08-19 | 62 | **+60.29** | 60 | **−161.68** | **+221.97** |
| 2026-08-20 | 91 | +59.31 | 93 | +122.65 | −63.34 |
| 2026-08-21 | 85 | −237.88 | 89 | −221.26 | −16.62 |
| 2026-08-22 | 98 | −56.27 | 93 | −144.52 | +88.25 |
| 2026-08-23 | 137 | +102.12 | 135 | +98.12 | +4.00 |
| 2026-08-24 | 73 | +71.02 | 95 | +117.19 | −46.17 |

**On 2026-08-19 "the day" is +$60 or −$162 depending on which tool you ask, sign
included.** This is the same failure mode as the four charter bugs: two tools that
appear to measure the same thing do not.

Secondary: `kstat.py` hardcodes `D.timezone(D.timedelta(hours=-4))`. That is EDT. It
will be silently one hour wrong from 2026-11-01, which moves the 23:00-00:00 ET
boundary again.

---

## 7. FINDING M3 — `scripts/backtest.py`'s headline includes series the bot does not trade

`backtest.load()` reads every `data/candles/*.csv.gz` row. The archive also holds
`KXWTI15M`, `KXGOLD15M` and `KXSILVER15M`, which are `SHADOW_SERIES`. `reconcile.py`
filters to `SERIES_LIST` and its docstring names this exact hazard — the fix went into
reconcile and never into the harness it calls canonical.

```
python3 scripts/backtest.py --slip 0.105          # what gets quoted
python3 scripts/verify.py --check harness         # same run, live series only
```

| | trades | WR | total | $/tr | $/day |
|---|---|---|---|---|---|
| `backtest.py` as shipped (all archived series) | 9383 | 93.54% | +2624 | **+0.280** | +35 |
| live series only | 8900 | 93.62% | +2657 | **+0.299** | +36 |

The headline understates per-trade EV by **$0.019 (−6.4%)** and inflates the trade
count by 5.4%. Every capture percentage computed against it has an inflated
denominator.

Note that `+0.299` — the live-series figure — is the number CLAUDE.md quotes as the
v5.17 baseline. So the pre-registration was computed with a filter the shipped harness
does not apply, and `backtest.py` cannot reproduce the number written next to it.

**Related, and correct:** the archive's candle indexing was checked and is sound.
`archive_candles.py` builds `prior_k` as `candles[i−k]`, which is only a k-minute
lookback if the API returns contiguous minutes. It does:

```
python3 scripts/verify.py --check archive
```
→ `1249/1249 consecutive candle_idx pairs are exactly 60s apart (100.000%)`

---

## 8. FINDING M4 — the v5.17 arithmetic reproduces; its confidence interval does not

CLAUDE.md l.432 pre-registers: `+$2,657 → +$3,101, delta +$443, CI [+68, +826], P=0.987`.

`backtest.py` cannot express a side-asymmetric band at all, so the claim was
re-derived by mirroring `qualifies()` with a per-side `min_ask`
(`python3 scripts/verify.py --check v517`):

| | trades | WR | total | $/tr |
|---|---|---|---|---|
| live as coded (yes≥90, no≥90) | 8900 | 93.62% | +2657 | +0.299 |
| v5.17 intent (yes≥88, no≥90) | 9172 | 93.50% | **+3101** | **+0.338** |
| symmetric (yes≥88, no≥88) | 9447 | 93.03% | +2882 | +0.305 |

**The point estimates reproduce exactly** — +$2,657 → +$3,101, delta +$443, P(better)
= 0.986 vs the claimed 0.987. The side-asymmetry argument holds: symmetric is worse
than baseline, which is why every `MIN_ASK` sweep found nothing.

**The interval does not reproduce.** `B.bootstrap` over six seeds and two iteration
counts:

| seed / iters | 98.75% CI |
|---|---|
| 7 / 3000 | **[−6, +859]** |
| 1 / 3000 | [+2, +882] |
| 42 / 3000 | [+0, +879] |
| 7 / 10000 | [+3, +875] |
| 1 / 10000 | [−3, +888] |
| 42 / 10000 | [+10, +874] |

The lower bound sits on **zero ± 10**, never at +68. The claim "CI excludes zero" is
resampling noise either way — the change is *at* the harness's significance bar, not
past it. That does not make v5.17 wrong; it makes it unestablished, which matters
because it shipped as pre-registered and validated.

**And it is moot until the gate is fixed:** no order can be priced below 90c
(`LIVE_SPEC.md` §6). The pre-registered horizon — "200 88-89c-YES trades, ~54 days,
do not read the subset before then" — will never be reached, and the decision rule
attached to it ("revert if the 88-89c YES subset WR is below 88.5% at n=200") can
never fire. At today's ~135 trades/day the forgone value is roughly
`0.039 × 135 ≈ +$5/day`, if and only if the last-look gate is corrected first.

**The one claim in the v5.17 row that fully survives** is executability, and it
reproduces from live data rather than the archive. CLAUDE.md: "84% executable
(depth>=60) versus 11-15% for the 94-96c candidates."

```
python3 -c "
import csv,glob
d=[r for c in sorted(glob.glob('data/gatelog/*.csv')) for r in csv.DictReader(open(c))
   if r.get('depth') not in ('','None')]
g=[r for r in d if 88<=float(r['ask'])<=89.999 and r['side']=='yes']
print(len(g), sum(1 for r in g if float(r['depth'])>=60)/len(g))"
```
→ `70 0.842` — **84.3%**, matching to a tenth of a point, and unchanged at the live
threshold of 39 (also 84.3%). By comparison the live band measures 59.8% (YES) and
53.8% (NO) on the same log.

Note the threshold in that sentence is the legacy `MIN_BOOK_DEPTH = 60`; the live gate
needs 39 (`LIVE_SPEC.md` §7.5). Here it happens not to matter. It does matter for the
Aug 23 depth-gate conclusion — "`depth==0` is 88% of every block … `MIN_BOOK_DEPTH=60`
is a near-perfect availability detector" — which is an argument about a threshold the
trader stopped using when the bet was cut to $25.

---

## 9. Capture, EXTRA and the P&L gap

`python3 scripts/reconcile.py --since 2026-08-19`

| bucket | n | WR | live $ | model $ | live $/tr |
|---|---|---|---|---|---|
| MATCHED | 292 | 93.84% | +152.34 | +139.10 | +0.522 |
| MISSED | 335 | 93.43% | — | +95.11 | — |
| EXTRA | 178 | 87.64% | **−459.35** | — | −2.581 |
| LIVE TOTAL | 470 | | **−307.00** | | |
| MODEL TOTAL | 627 | | | +234.21 | |

Capture **46.6%**. On the clean day 2026-08-23 alone it is **65.3%**, EXTRA is
**+$8.58**, and live total +$98.11 matches the settlements API's +$98.12 to a cent.

### EXTRA is mostly a measurement asymmetry, not a selection bug

Splitting the 178 by why the model rejected them
(`python3 scripts/verify.py --check extra`):

| cause | n | WR | P&L | $/tr |
|---|---|---|---|---|
| in band at a candle close, but failed a prior gate | 66 | 89.39% | −77.36 | −1.17 |
| **never in band at any candle close** (transient — the bot polls, the archive samples) | 55 | 89.09% | −129.00 | −2.35 |
| qualified in the archive, lost the 2-slot allocation | 54 | 87.04% | −158.27 | −2.93 |
| not in the archive at all | 3 | 33.33% | −94.71 | −31.57 |
| *MATCHED, for comparison* | 292 | 93.84% | +152.34 | +0.52 |

Two things follow, and they point in opposite directions from the raw −$459:

1. **The worst bucket is "qualified, lost slot" (87.04%)** — trades the model itself
   would happily take, differing only in which two of a cluster got the slots. A
   *selection* defect cannot explain that. Against MATCHED it is z = −1.77; the
   transient bucket is z = −1.27. Neither sub-bucket is significant.
2. **The 3 "not in archive" trades carry −$94.71 of the −$459** — 21% of the total on
   1.7% of the trades, at $50-75 bet sizes. This is variance in a small window
   measured at a stale bet size, not a rate.

**Live win rate is not established as below the model's:** 91.49% over 470 trades
against the model's 93.54% is z = **−1.81**, and trades inside a close cluster are
correlated, so the true interval is wider still. The −$541 gap over Aug 19-23 is
dominated by five $50-75 losing days, not by edge decay.

### The last-look gate is a bigger filter than anything in CLAUDE.md's funnel

The gate log carries both the quoted ask and the book's best offer, so the last-look
gate's bite is directly measurable for the first time (615 rows with a book read):
**best_offer sits outside [90,93] on 63% of in-band YES quotes and 58% of NO quotes.**
The book disagrees with the listing by more than 3c on 14.5% of observations, in both
directions (median +0.50c, p10 −4.80c, p90 +3.50c).

CLAUDE.md's Aug 24 funnel lists "heat check 51%, ask-moved 74, priors-failed 16, thin
book 6" and concludes the concurrency cap is the dominant blocker. Last look is not a
category in that funnel at all. Full table in `LIVE_SPEC.md` §6.

### Concurrency cap — CHECKED, correctly enforced
```
python3 scripts/verify.py --check cap
```
→ `0 of 298 close clusters exceed MAX_CONCURRENT_POSITIONS=2` (126 clusters at 1
position, 172 at 2). Not an over-trading bug.

---

## 10. Fill quality — the three sources disagree in sign

| source | figure | window |
|---|---|---|
| CLAUDE.md l.175, and `backtest.py --slip` help text | **+0.105c adverse** | measured 2026-08-17 |
| `reconcile.py`, Aug 19-23 matched | live 91.501c vs model 91.564c = **−0.062c (favourable)** | 2026-08-19..23 |
| `reconcile.py`, Aug 23 matched | **−0.128c (favourable)** | 2026-08-23 |
| settlements API, realised avg fill | 91.42c | Aug 15-24 |

The +0.105c constant is **7 days stale and now disagrees in sign** with the tool that
measures it. Every backtest quoted "at the measured fill quality" — including the
v5.17 pre-registration and the `max_conc` sweep — is running a penalty the current data
does not support.

Caveat, and it is the trader's own (l.1470): comparing a fill to a 1-minute candle
"yields a +0.85c artifact from regression to the mean because the 1-min reference is
stale 47% of the time". `reconcile.py` does exactly that comparison. So the honest
statement is **fill quality is currently unmeasured**, not "fill quality is +0.105c",
and `[EXEC]` log lines are the instrument that would settle it.

---

## 11. Source disagreements, in dollars

| # | Disagreement | Worth | Which is right |
|---|---|---|---|
| 1 | Dashboard counts any `*15M` ticker as strategy; reconcile uses `SERIES_LIST` | **307 trades, −$245.05** (HYPE, NEAR, WTI) | **reconcile.** Those series are shadow/retired; the bot places no orders in them. |
| 2 | `backtest.py` scores all archived series | +$0.019/tr, +5.4% trades | **filtered.** GOLD/SILVER/WTI are `SHADOW_SERIES`. |
| 3 | Day key: ET (kstat, dashboard) vs UTC (reconcile, archive) | up to **$222 and a sign flip** on one day | **UTC**, because the archive is keyed that way and the backtest is the comparison target. kstat and the dashboard should follow it. |
| 4 | `kstat` win = `pnl > 0`; everyone else = `market_result == side` | **1 row in 1948** — `KXBNB15M-26AUG140930-30` settled a win at exactly `pnl = $0.00`, which `pnl > 0` scores as a loss | `market_result`. Rare but not theoretical: `>` should be `>=`, or better, read the outcome. |
| 5 | Non-15M account activity | **−$879.04** (KXMLBTOTAL) | Correctly outside strategy stats, but it dominates account P&L and must never be dropped from a balance line. |
| 6 | C1 quarantine: trader has no side test, `backtest.qualifies` has `side == "yes"` | 4 NO-side SOL entries in 3 days appear as MISSED | **trader is the fact**; the harness comment claiming it matches is false (`LIVE_SPEC.md` §7.4). |
| 7 | Empty-book NO priors: trader → 100c (passes), archive → blank → −1.0 (fails) | 0.63% of NO rows | **archive.** Zero means "not on offer" (`LIVE_SPEC.md` §7.6). |
| 8 | Gate log writes `min_depth=60`; live gate needs 39 | over-counts depth blocks | **39** (`LIVE_SPEC.md` §7.5). |

---

## 12. Checked and clean

Recorded so the Session 2 diff can distinguish "verified" from "not looked at".

| Node | Verdict |
|---|---|
| State artifact vs settlements API, every retained position | **exact** — 0 P&L, 0 cost, 0 fee mismatches across all 500 (`verify.py --check state`, which re-derives the totals so they do not rot here) |
| `fee_cost` / `revenue` / `*_dollars` units | **correct** (§2) |
| Realised fee rate vs the 0.54pp figure inside break-even | **0.528c/contract — confirms it** |
| Archive candle contiguity (`prior_k` really is k minutes) | **1249/1249 pairs at exactly 60s** |
| `MAX_CONCURRENT_POSITIONS` enforcement | **0 of 298 clusters over cap** |
| `kstat.margin` vs dashboard `margin()` on identical rows | **agree to <0.01pp** |
| Deployed bet size = `FLAT_BET_DOLLARS` = $25 | **confirmed** — 230 trades, cost $23.48-$24.96 |
| reconcile live total vs settlements API, 2026-08-23 | **+98.11 vs +98.12** (rounding) |
| YES vs NO edge in the clean window | **no difference established** (z = 0.73) |
| Live WR below the model's | **not established** (z = −1.81) |
