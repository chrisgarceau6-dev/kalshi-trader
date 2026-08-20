# Pre-registration — can perps improve the 15M strategy?

Written **before** running any of these tests. Archive: 2026-06-11 → 2026-08-19.

**Split.** In-sample (IS) = 2026-06-11 → 2026-07-31. Holdout (OOS) = 2026-08-01 → 2026-08-19.
Every hypothesis is formed on IS and confirmed on OOS. A result that does not hold on
OOS is recorded as refuted, not "promising".

**Resampling.** Close cluster, never trade (CLAUDE.md invariant 3). All CIs are
cluster bootstrap, 3,000 iterations, 97.5% two-sided.

**No look-ahead.** Every spot input uses the close of the minute *before* the entry
minute. A concurrent-minute variant is reported separately as an upper bound on what
a live implementation could capture.

**Primary metric.** `won − ask/100`, in percentage points — the edge *beyond what the
Kalshi ask already prices*. A signal that only reproduces the ask is worthless.
Secondary: $/trade at the live $50 bet.

---

## Signal hypotheses — perps/spot as information (no perp position taken)

**H1 (primary).** Vol-normalised distance to the strike predicts outcomes beyond the ask.
`z = ln(S/K)·sign / (σ·√((τ−40)/60))`, σ = sd of trailing 60 one-minute log returns,
τ = secs_left. The −40 accounts for settlement being a 60s BRTI average, not a point.
*Predict:* `won − ask/100` increases monotonically in z. Top z-quintile beats bottom
by >1pp, IS and OOS, CI excluding zero.

**H2.** Adverse spot momentum before entry predicts losses.
`m = −sign · (3-minute spot return) / (σ·√3)`.
*Predict:* high adverse momentum → worse; monotone decreasing in m.

**H3.** Trailing realised-vol regime predicts losses. Bucket by σ (per-series percentile).
*Predict:* high vol → worse. Note: a Kalshi-candle dispersion filter was refuted in
§8; this is a different measurement (spot, not ask) and gets exactly one test.

**H4.** Cross-asset — BTC's 3-minute move at the time of an alt entry, in the alt's
adverse direction, predicts alt losses beyond the alt's own spot.
*Predict:* adverse BTC move → worse alt outcomes.

**H8.** z lets the ask band widen. Among archived rows at ask 94–96¢ (currently excluded)
and 88–89¢, is the edge positive conditional on high z?
*Predict:* if H1 holds, high-z 94–96¢ entries are +EV despite the aggregate being flat.

**H9.** When both sides / several markets qualify in one cluster, ranking slots by z
beats the current rank by secs_left.

**H10.** z dominates the existing `PRIOR_MIN=75` prior-candle gate — i.e. conditioning
on z, the prior-candle gate adds nothing and can be removed.

## Hedge hypotheses — perps as a position

**H5.** A properly delta-sized static hedge (per-trade `Q_i = C·φ(z)/(σ√τ_eff)`, not a
fixed notional) improves risk-adjusted P&L net of 5bp round-trip.
*Predict:* fails — overlay EV is zero by construction, cost is not.

**H6.** One cluster-level BTC perp, sized to the aggregate delta of open positions,
achieves the same variance reduction at a fraction of the fee load.

**H7.** Dynamic: open a perp only when spot crosses to the adverse side of the strike
mid-life, in the direction that is now winning, held to close. Trades in ~15% of
positions, so fee load is ~7x lower than a static overlay.

## Kill criteria

- Any hypothesis whose OOS effect has the opposite sign to IS is refuted, full stop.
- A filter that removes >40% of volume needs its total-P&L effect reported, not just
  per-trade — the strategy is a volume business (§7: excluding high-dispersion entries
  cut holdout P&L from +$4,242 to +$992 while "improving" per-trade stats).
- Any hedge that costs more than +$0.59/trade gross is dead regardless of variance.

---

# Results (filled in after running — see git history for the pre-registered version above)

| # | Hypothesis | IS | OOS | Verdict |
|---|---|---|---|---|
| H1 | vol-normalised distance to strike `z` | +1.86pp, P=0.94 | **-0.22pp** | REFUTED (sign flip) |
| H2 | adverse 3-min spot momentum | -3.94pp, CI excl. 0 | -1.59pp, same sign | **SURVIVES** |
| H3 | trailing realised-vol regime | -0.65pp | **+4.30pp**, P=1.00 | REFUTED (sign flip) |
| H4 | cross-asset BTC 3-min move | -2.37pp | -0.11pp | REFUTED (vanishes) |
| H5 | delta-sized static perp hedge | — | — | REFUTED (loses at 0bp) |
| H6 | cluster-netted BTC hedge | — | — | REFUTED (only 4% nets) |
| H7 | dynamic hedge on adverse crossing | — | — | REFUTED (no sd cut) |
| H8 | widen band using the signal | 94-96c dead; 88-89c +0.55/tr | 88-89c +0.84/tr | NOT ESTABLISHED (dies at 1 tick) |
| H9 | rank cluster slots by signal | better | **worse** | REFUTED |
| H10 | signal dominates prior-candle gate | much worse | much worse | REFUTED |

H1b (`z` from the minute AFTER entry) scored +14.19pp IS / +7.26pp OOS. That is 60
seconds of look-ahead, not a signal: `secs_left = close_ts - end_period_ts`, so the
archive's observation instant is `close_ts - secs_left` and the concurrent Coinbase
bucket closes 60s later. Recorded here only because the size of it is a good reminder
of how much a look-ahead bug can manufacture.
