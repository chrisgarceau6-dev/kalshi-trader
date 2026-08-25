# Deep taker scan + the last untested corners — 2026-08-25 (session 2)

Ran after the maker search (`MAKER.md`) turned up the fact that made this necessary:
**the 15M half of the step-3 archive is ~8 days deep, not 67.** `pull.py` caps at 600
markets and a 15M series runs 96 markets/day. The original scan's verdict — "9 cells
cleared a 95% CI in-sample, ZERO survived the holdout" — is sound for the weather
ladders that supply most of its 14.2M observations, and was never really tested on the
twelve series that are the only ones with minute-by-minute flow.

`data_ohlc/` now holds **60,342 markets over 68 days** for those twelve, roughly 10x
the history, with OHLC rather than closes alone. This is that re-run, plus the two
corners CLAUDE.md still listed as untested.

**VERDICT: still nothing implementable.** One cell survived the holdout and it is a
filter on the strategy already live, which loses money when you act on it.

---

## 1. The deep scan — 836 cells, 1 survivor

`deep_scan.py`. Leads taken from the in-sample 70% of close clusters only, frozen,
then scored once on a holdout the search never saw. Everything resamples close
clusters (invariant 3).

> 836 cells tested in-sample. **4** cleared a 95% CI — where noise alone would give
> about 42. (Fewer than noise, because the cluster bootstrap is severe when twelve
> series settle together.) Of those 4, **1** survived the holdout.

| lead | IS net | OOS n | OOS net | OOS 95% CI |
|---|---|---|---|---|
| 10-24c / prior60-74 | +3.54 | 366 | -0.36 | [-4.56, +3.80] |
| **90-93c / prior75-89 / calm** | **+2.04** | **1,677** | **+2.26** | **[+0.70, +3.71]** |
| 01-09c / wild | +0.78 | 5,076 | +0.96 | [-0.02, +2.12] |
| 01-09c / prior<40 / wild | +0.71 | 4,735 | +0.69 | [-0.32, +1.76] |

`calm` = `rv3 <= 8`, where rv3 is the mean of `ask_high - bid_low` over the preceding
three minutes. Strictly ex-ante.

The survivor replicates unusually well — full window **+2.14c, CI [+1.11, +3.10]**,
n=3,649, and **11 of 12 series positive** (only DOGE negative, at -0.42c).

### And it is not actionable, for the reason the dispersion filter was not

It is a slice of the LIVE band. Applying it as a filter, on the exact live geometry
across the six live series, 68 days, $25 flat:

| subset | n | share | net c/ct | $ total | $/day |
|---|---|---|---|---|---|
| all (what runs today) | 18,813 | 100% | +0.53 | **+$2,764** | +$40.81 |
| calm only | 3,487 | 18.5% | +1.12 | +$1,074 | +$15.85 |
| NOT calm | 15,326 | **81.5%** | +0.40 | **+$1,690** | +$24.95 |

**Filtering to calm costs $1,690 over 68 days**, and the same on holdout
(+$972 -> +$610). This is the identical arithmetic that killed the dispersion filter:
the excluded entries are most of the volume and most of the profit, and their edge is
positive, just smaller. Invariant 7 again — removing restrictions beats adding them.

**The one framing under which it is not worthless** is sizing rather than exclusion.
Normalised to the same average bet, so this compares shape and not leverage:

| scheme | $/day full | $/day holdout |
|---|---|---|
| flat $25 (today) | +$40.81 | +$49.71 |
| calm 1.5x / other 0.75x | +$47.80 | +$68.24 |
| calm 2x / other 0.5x | +$56.78 | +$92.06 |

These two tables are not in conflict, and the difference matters: **filtering** loses
money because the trade simply is not taken, while **tilting** keeps every trade and
moves capital toward the better half. Which one is the right question depends on
whether capital or slots binds — and for late-certainty it is **slots**
(`MAX_CONCURRENT=2`), not capital, so the filter framing is the honest one and the
answer is don't. The tilt is a live-trader change, is Chris's call and nobody else's,
and belongs with the existing time-weighted-sizing thread, which already needs
122-279 days. Do not treat the holdout column as a reason to hurry.

## 2. FX and index — the last "genuinely untested corner", now closed

CLAUDE.md's archetype filter left "FX/index hourly (KXINXU, KXEURUSDH) — the one
genuinely untested corner". Tested.

**KXINXU (hourly, 40 strikes, 612,920 volume)** passes the population screen that
closed weather — 10.5% of quotes in 88-96c, ~212 in-window observations/day, against
171-178 for BTC/ETH. It then produces nothing:

| band | 150-600s | 10-30m | 30-60m |
|---|---|---|---|
| 90-93c | **-2.46** | +0.12 | +0.15 |
| 94-96c | **-2.74** | +0.58 | +0.11 |
| 97-99c | **-1.66** | -0.13 | -0.33 |

Every CI includes zero, and the cells nearest live geometry are the negative ones.
With the live prior gate: 88-89c +0.07c, 90-93c -0.16c, 94-96c +0.11c. Nothing.

**KXEURUSD (daily)** is worse — -6c to -18c through the 88-93c band, winning 71-86%
at prices implying 88-93%.

The structural reason is Invariant 2: **median spread is 3.0c on KXINXU and 4.0c on
KXEURUSD against 1.0c on crypto 15M.** One cent is about half the edge here, so two to
three extra cents of spread is the entire thing before any question of edge arises.
Note also the cluster arithmetic — KXINXU gives 126 close clusters in 25 days and
KXEURUSD 46 in 64 days, so these can never be established quickly however many
strike-minutes they contain.

## 3. First per-trade numbers for KXZEC15M and KXNEAR15M

Neither had one. CLAUDE.md lists NEAR under "not currently pursued" with no figure and
does not mention ZEC at all. Live geometry, 56 days each:

| series | live? | n | WR | net c/ct | 95% CI | $/day |
|---|---|---|---|---|---|---|
| KXETH15M | LIVE | 3,088 | 93.4% | **+1.51** | [+0.27, +2.64] | +$18.91 |
| KXBNB15M | LIVE | 3,059 | 92.9% | +0.88 | [-0.36, +2.05] | +$11.04 |
| KXDOGE15M | LIVE | 3,342 | 92.4% | +0.43 | [-0.85, +1.71] | +$5.89 |
| KXWTI15M | - | 812 | 92.5% | +0.42 | [-2.38, +2.94] | +$4.30 |
| KXBTC15M | LIVE | 3,200 | 92.2% | +0.30 | [-0.96, +1.49] | +$3.74 |
| KXSILVER15M | - | 1,005 | 92.3% | +0.26 | [-2.40, +2.73] | +$2.89 |
| KXXRP15M | LIVE | 3,209 | 92.1% | +0.16 | [-1.14, +1.41] | +$2.07 |
| KXSOL15M | LIVE | 2,915 | 91.8% | -0.07 | [-1.44, +1.20] | -$0.81 |
| KXHYPE15M | - | 3,149 | 91.1% | -0.80 | [-2.20, +0.51] | -$10.05 |
| KXGOLD15M | - | 1,036 | 90.6% | -1.35 | [-4.07, +1.21] | -$15.45 |
| **KXZEC15M** | - | 3,064 | 89.2% | **-2.85** | **[-4.36, -1.40]** | -$42.56 |
| **KXNEAR15M** | - | 3,466 | 87.3% | **-4.55** | **[-6.19, -2.96]** | -$77.21 |

**ZEC and NEAR are reliably NEGATIVE** — CIs exclude zero on both. Do not add them.
Pooled, the six non-live series are **-2.22c, CI [-3.01, -1.42], P(>0)=0.000**, against
the live six at +0.53c [-0.14, +1.18]. **The series selection that is live is correct**,
and that is now measured rather than assumed. Silver (+0.26c here, over 25 days) is
consistent with the +0.32/tr CLAUDE.md records and remains a late-September calendar
item, not a finding.

## What is now exhausted

Passive/maker (12 series, `MAKER.md`) · the taker scan at 10x the 15M history ·
FX and index, hourly and daily · weather and the commodity dailies (population) ·
every 15M series that exists. The structural reason the search keeps returning zero is
arithmetic and worth stating plainly: **a taker pays ~1c of spread plus 0.5-1.75c of
fee, and the mispricings in this venue are ~1-2c.** The live strategy clears that bar
by about half a cent per contract, and nothing else found clears it at all.
