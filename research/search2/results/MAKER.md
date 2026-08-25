# Maker entries — the half of the search space scan.py could not see

Run 2026-08-25. Tooling: `pull_ohlc.py`, `maker_scan.py`, `maker_eval.py`,
`maker_sim.py`. **VERDICT: refuted. Nothing survived.**

---

## Verdict first

The maker rule that looked strongest — rest a bid on the favourite side of a 15M
market, hold to settlement, never pay a fee — measures, across all twelve 15M series,
68 days, 109,278 modelled fills:

> **+0.10c per contract, 95% CI [-0.47, +0.65], P(>0)=0.630.**
> **8 series positive, 4 negative. Drop KXBNB15M alone and the pooled figure is -0.23c.**

That is zero. It joins the GRAVEYARD.

**How this nearly became a false positive, recorded because the mechanism matters
more than the result.** At six series it read **+0.98c, CI [+0.24, +1.67], 6 positive
/ 0 negative**, stable in and out of sample. It was wrong because *the six were not a
random six* — the pull was ordered by which series I expected to work
(Silver, ETH, Gold, WTI first). The denominator was contaminated by the order the data
arrived in. CLAUDE.md already records this exact failure once (the 97-99c/prior>=90
pattern, 22 positive / 21 negative, from reading a leads list that only prints
positives). **Checking the denominator is not enough if you also chose the numerator's
order. Pull in a fixed or random order, and do not read the table until it is full.**

## The kill, in the order the tests fell

**1. The series genuinely differ — and it does not persist.**
Cochran Q=32.6 on 11 df, **p=0.0007, I²=66.2%**: the spread between series is real,
not sampling noise. So a subset might be tradeable. It is not — splitting each series
at its own midpoint, the sign agrees across the two halves in **6 of 12 series**,
which is exactly the coin flip, at a first-vs-second-half correlation of **+0.225**.

| series | first half | second half | | series | first half | second half |
|---|---|---|---|---|---|---|
| BNB | +2.48 | +1.52 | | NEAR | -0.82 | -1.77 |
| BTC | +1.62 | **-1.72** | | SILVER | +0.62 | +5.55 |
| DOGE | -0.23 | +0.53 | | SOL | +1.78 | **-0.75** |
| ETH | +0.62 | **-0.81** | | WTI | +1.65 | +3.33 |
| GOLD | +0.63 | 0.00 | | XRP | +0.83 | **-0.57** |
| HYPE | -0.24 | +0.10 | | ZEC | -2.84 | -0.41 |

Which series pays is a regime, not a property. **No subset can be selected forward**,
and selecting one backward is the KXETH15M-exclusion error that would have cost $978.

**2. Two series are reliably NEGATIVE.** KXZEC15M -1.65c [-2.87, -0.42] and
KXNEAR15M -1.31c [-2.61, -0.05]. With twelve series at a 95% level, three positives
(BNB, SILVER, WTI) and two negatives is what noise plus heterogeneity produces.

**3. The portfolio never got there.** 10 contracts, $2,000 cap, 8 per close cluster:
+$141/week with a CI of **[-$256, +$537]**, **P(>0)=0.754**, 38/67 days positive,
worst drawdown $1,734. The point estimate flatters a distribution that is
indistinguishable from zero.

## What IS established, and is worth keeping

**Maker fills are free — 15x the evidence CLAUDE.md carries.**
`/portfolio/fills`, 2026-07-24..08-25:

| | fills | contracts | total fee | per contract |
|---|---|---|---|---|
| maker | 855 | **15,159** | **$0.00** | **0.0000c** |
| taker | 6,821 | 74,575 | $446.21 | 0.5983c |

(CLAUDE.md cites 998 maker contracts.) Seven maker fills carry $0.00002 of rounding
dust. Units pinned against a single fill: 27 ct at 90.50c, `0.07*0.905*0.095*27 =
$0.1625`, matching `fee_cost: "0.162500"` — so `fee_cost` is DOLLARS and `count_fp`
is contracts. **The free-maker fact is true. It is simply not enough.**

**Adverse selection is exactly the size of the prize.** Fills separate cleanly by how
far the market traded through the resting bid (12 series):

| sweep depth | share of fills | net |
|---|---|---|
| gentle 0-1c | ~22% | **+10c** |
| 1-3c | ~16% | +7c |
| 3-6c | ~17% | +5c |
| swept 6c+ | **~45%** | **-7c** |

You cannot choose which you get, and the blend is zero. This is the general reason a
resting order earns nothing here, and it will kill the next maker idea too.

**Size must never scale with liquidity.** Fill size follows the minute's volume, and
the highest-volume minutes are the deep sweeps. Let size follow volume and the same
rule measures **-5.05c**; cap it at <=100 contracts and it measures **+4.80c** on the
same fills. This is the one parameter whose mis-setting inverts the sign rather than
shrinking the edge — worth remembering for any future passive strategy.

**The mid carries a real favourite-longshot bias, and it is not harvestable.**
Cluster-bootstrapped, 15M series: the favourite side is underpriced by **+1.25c**
(55-74c) and **+1.36c** (75-89c), the longshot side overpriced by -1.28c (05-44c) —
the same fact twice. A taker cannot have it: at 60c, +1.18c of bias against a 0.83c
half-spread and a 1.68c fee is **-1.33c**, which is why the taker scan finds nothing.
A maker keeps the bias and the half-spread, and then hands both to adverse selection.

**Population closes weather and the commodity dailies for ANY maker strategy.**
Fraction of minutes containing no trade at all:

| group | empty minutes |
|---|---|
| 15M series (12) | **0.0%** |
| KXBTCD hourly | 58% |
| weather dailies | 60–89% |
| commodity dailies (WTI/GOLD/BRENT daily) | 84–90% |

No trades, no fills. This is independent of the archetype filter that closed weather
for late-certainty, and it closes the same ground for a different reason. The 78.4M
weather volume is spread over 408 markets x 67 days and is not there minute to minute.

## Two mechanisms proposed and falsified

- **The fee-moat story is wrong.** `0.07*P*(1-P)` is maximised at 50c, so the
  no-arbitrage band protecting a resting quote should be widest there and the maker
  edge largest at 50c. Measured, **45-54c is the one dead band** (-0.29c).
- **It is not a liquidity effect.** Spearman(median minute volume, mid bias) across
  12 series = **0.112**. Nothing.

## Negative results worth not repeating

- **Resting orders do not improve late-certainty.** On live geometry (90-93c,
  priors>=75, 150-600s) only **406 of 3,088** KXETH15M signals (13%) can even host a
  `bid+1` order — the spread there is 1c, so `bid+1` is the ask and the order crosses.
  On the ones that can, the maker nets +1.33c against the taker's +1.51c. There is no
  execution upgrade available in that band.
- **Both archives were 10x shallower than they looked.** `pull.py --max-markets 600`
  and the first `pull_ohlc.py` run capped at ~700 markets, which for a series running
  96 markets/day is **7 days, not the 67-day retention window**. Every 15M figure in
  the step-3/step-4 scan output rests on ~8 days. On 7 days this rule measured +6.4c;
  on 68 days it measures +0.10c. **Check the date span of any 15M pull before
  believing it.** `pull_ohlc.py` now defaults to 8,000 markets and threads the fetch
  (6 workers, ~12 min/series, 0 network retries over 7,011s).

## Checks the pipeline had to pass, and did

- **It rediscovers late-certainty unprompted.** Live geometry as a taker on KXETH15M
  scores **+1.510c, CI [+0.27, +2.64]** against the +1.71c CLAUDE.md records for
  `scan.py`. A scan that cannot find the strategy already known to work cannot be
  trusted; this one finds it.
- **Not a coding artifact.** An independent re-implementation sharing no code, with an
  adjacent-minute guard on the candle join, reproduces the headline cell to 0.02c.
- **Not settlement foreknowledge.** The edge is largest early and weakest in the final
  two minutes — the opposite of late-certainty. The final two minutes are excluded
  throughout anyway.
- **Not queue position.** Requiring the market to trade STRICTLY through the bid, so
  the fill lands regardless of queue rank, does not move the cell.
- **The conservative placement was the stronger one**, which is the right way round:
  joining the existing bid (back of queue, fill only once the level is cleared) beat
  improving the quote at every band tested.

## If anyone reopens this

Only with NEW data, and only these two doors:

1. **A pre-registered forward test on a FIXED series list chosen before looking** —
   the per-series table above may not be used to pick it. Note the power: on KXETH15M
   the weekly mean is +1.07c against a weekly sd of **3.71c**, so ~75 trading days is
   the honest horizon for a single-series read.
2. **An ex-ante predictor of the deep sweep.** The one that replicated is `rv3`
   (mean intra-minute range over the preceding 3 minutes): sweep rate rises monotonely
   with it, 42%->59% in-sample and 38%->55% on a frozen holdout. It roughly doubles
   the per-contract edge and cuts fills more than half, so it *loses* dollars — but it
   is a real, replicated handle on the thing that does the killing, and a better
   predictor would change the arithmetic rather than merely re-slicing it.

Do not reopen it on the strength of the free-maker fact alone. That fact is true, was
the reason to look, and is fully priced by the spread the maker has to rest inside.
