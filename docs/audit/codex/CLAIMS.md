# Tier C claim audit — CLAUDE.md §§7–8

Audit date: 2026-08-24. Scope: every observation row in §7, the crash-fill prose immediately below it, and every claim family in §8. This is an evidence audit, not a new strategy search. `CLAUDE.md` is treated as an index of assertions, never as proof.

## Verdict vocabulary

- **HOLDS** — the surviving producer and present data support the decision actually taken.
- **PARTIAL / STALE** — the direction remains supportable, but the quoted magnitude, units, population, execution assumption, or live-rule mapping does not.
- **FAILS** — the surviving evidence contradicts the claim or does not test the live rule it purports to justify.
- **UNREPRODUCIBLE** — no committed producer plus inputs was found for the result. A prose row or commit message is not a producer.
- **NOT LIVE BASIS** — exploratory or superseded material that justifies no current live decision.

“Producer” below means the script and the necessary durable input existed at audit time. A later audit script can independently test a proposition, but does not retroactively supply provenance for an earlier claim.

## Reproduction ledger

All figures and source-existence judgments below are produced by these commands. Run from the repository root. `PYTHONDONTWRITEBYTECODE=1` keeps the read-only runs from creating bytecode.

```bash
# R0 — source text and its history
nl -ba CLAUDE.md | sed -n '426,557p'
git blame -L 432,557 -- CLAUDE.md
git show --stat --oneline 584d0a85 ee5cadd4 66e664de 16dc457c 3b849178 7976944e a261d77e 156feeb9 77325fdd 0f8aef61 7b7feec7 d22512e5 97991104 d2329425 ef1dfda6

# R1 — durable producer/input inventory
git ls-files scripts research backtest_longshot.py kalshi_weather_edge.py | sort
git status --short -- backtest_acceleration.py backtest_breakout.py backtest_cross_asset.py backtest_series_pause.py backtest_ultralate.py
find research -maxdepth 3 -type f | sort
ls -l ~/Downloads/Finance/*kalshi_weather_edge*.py

# R2 — canonical harness, current archive, current measured execution assumption
PYTHONDONTWRITEBYTECODE=1 python3 scripts/backtest.py --slip 0.227
PYTHONDONTWRITEBYTECODE=1 python3 scripts/backtest.py --slip 0.227 --sweep min_ask 88 89 90 91
PYTHONDONTWRITEBYTECODE=1 python3 scripts/backtest.py --slip 0.227 --sweep max_conc 2 3 4 5 6
PYTHONDONTWRITEBYTECODE=1 python3 scripts/backtest.py --since 2026-08-18 --until 2026-08-18

# R3 — known harness mismatches and archive/execution defects
PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify.py --check slippage
PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify.py --check rounding gates harness

# R4 — current surviving Tier C producers
PYTHONDONTWRITEBYTECODE=1 python3 scripts/calibration.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/entry_timing.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/vol_bucket_test.py
PYTHONDONTWRITEBYTECODE=1 python3 research/loss_cooldown/measure.py
PYTHONDONTWRITEBYTECODE=1 python3 research/loss_cooldown/steelman.py
PYTHONDONTWRITEBYTECODE=1 python3 research/perp_overlay/s1_robustness.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=research/perp_overlay python3 research/perp_overlay/h_hedge.py
PYTHONDONTWRITEBYTECODE=1 python3 research/hourly_crypto/analyze_hourly.py
PYTHONDONTWRITEBYTECODE=1 python3 research/hourly_crypto/analyze_hourly2.py
PYTHONDONTWRITEBYTECODE=1 python3 research/top5/sizing_validate.py

# R5 — the v5.17 deployment defect and corrected post-merge evidence
git log -1 --format='%H %cI' 584d0a85
for r in 32760434238 32758954186 32757489283 32756022508 32754550981 32753071584 32751618426; do
  gh run view "$r" --log 2>/dev/null | grep -E 'Last look.*best YES offer (88|89).*outside \[90' || true
done
PYTHONDONTWRITEBYTECODE=1 python3 docs/audit/claude/probe_fix.py

# R6 — inventory omission quantified
find . -type f -not -path './.git/*' | wc -l
find . -maxdepth 1 -type f -name '_tmp_*.csv' | wc -l
find . -maxdepth 1 -type f -name '_tmp_*.csv' -exec stat -f '%z' {} + | awk '{s+=$1} END {print s}'
git ls-files '_tmp_*.csv' | wc -l
```

The slippage check requires the live-state artifact. If it is unavailable locally, it reports `SKIP`; with the artifact used for the audit it reports all-side `+0.227c`, `n=500`, `t=+6.6` against the documented `+0.105c` (R3). The rounding check currently reports that rounded selection adds trades and edge and is always optimistic; the selected-identity calculation in the same check yields `128/317 = 40.4%` disagreement on the two exact-cent days. These are short-window measurements, not correction factors for the older archive.

## Headline conclusions for live decisions

1. **v5.17 is not supported by a surviving producer and is not operating as specified.** Commit `584d0a85` contains the trader, documentation, and tests, but no research producer. The canonical harness still models a symmetric lower bound and includes shadow series. Its advertised confidence interval is therefore not reproducible from the committed research surface. More importantly, the deployed `try_trade` rejects the intended 88–89c YES entries at the later hard-coded 90c check. Post-merge logs and the real-function scratch probe reproduce that failure (R5). This is a fail-closed missed-opportunity defect, not evidence of negative live fills.

2. **The current C1 claim tests the wrong rule.** `scripts/backtest.py` makes C1 YES-only; the trader applies it to both sides. The live-rule removal estimate is `-$52`, CI `[-$310,+$191]`, not the documented YES-only `-$106`, CI `[-$321,+$91]` (R3 plus the command recorded in `CONSTANTS.md`). The verdict remains “not justified,” but the old proof is not admissible.

3. **Measured fill quality and rounding invalidate most precise pre-August-22 magnitudes.** The current fill gap is `+0.227c` adverse, not `+0.105c`; selection identity changes on `40.4%` of the exact-day union under archive-style rounding, always optimistically. Claims may retain a robust sign, but “at measured fill quality” and narrow dollar projections are stale unless rerun under R2/R3.

4. **No newly discovered active money-losing bug.** The v5.17 defect blocks entries. Several state/sizing sites would misclassify or mishandle an intended low-band fill if only the first blocker were patched, but the unchanged deployed blocker prevents that path today.

## §7 row-by-row audit

| CLAUDE row | Original producer at audit time | Live decision claimed | Verdict |
|---|---|---|---|
| 432 — v5.17 88–89c YES-only deployment | **ABSENT.** The introducing commit adds no producer. `backtest.py` cannot express side-specific band minima and includes three shadow series. | Deploy the lower YES band; preserve two slots; enforce its prospective rule. | **FAILS.** The quoted CI is not reproducible; slippage and rounded selection are stale; and the live path cannot reach the band (R2, R3, R5). The prospective trial has not started because intended entries are rejected. |
| 433 — rounding is a minor row/band effect | Original producer **ABSENT**; later `scripts/verify.py` is present. | Trust pre-exact archive claims with a small caveat. | **FAILS in magnitude and unit of analysis.** Selected trades, not rows, are the decision object. R3 finds `40.4%` identity disagreement and optimistic direction. This strengthens the stale verdict on `MIN_ASK_CENTS`, `PRIOR_MIN_CENTS`, and `PRIOR_LOOKBACK`. |
| 434 — EXTRA is a resolution artifact | **ABSENT** for the stated diagnosis; `scripts/reconcile.py` is only a partial successor. | Treat EXTRA as coverage, not quality; retain the pre-registered revert. | **UNREPRODUCIBLE / PARTIAL.** The exact classified population and comparison were not committed. The revert remains procedurally supported by its pre-registered rule, independent of the missing profitability story. |
| 435 — complete capture is worth about negative three dollars | **ABSENT.** | Do not optimize raw capture percentage. | **UNREPRODUCIBLE.** The classified misses and purchasability snapshot are not durable. The conclusion may be sensible but cannot justify a live gate. |
| 436 — concurrency blocks half and costs nothing | Producer for run-log sample **ABSENT**; `backtest.py` survives for the sweep. | Keep `MAX_CONCURRENT_POSITIONS=2`. | **PARTIAL / STALE.** At measured slippage, R2 gives two slots `+$2,330`, three `+$2,244`; the sign of the two-to-three comparison supports keeping two, but the documented “loses $14” and `+0.105c` premise are stale. Larger caps are non-monotone, so “strictly” is too strong. Risk exposure independently favors the conservative choice. |
| 437 — experiment 152 failed its pre-registration and was reverted | Numerical producer **ABSENT**; commit history and revert are durable. | Keep the revert; require a fresh prospective test. | **HOLDS procedurally; numerical rescue UNREPRODUCIBLE.** The fixed rule, result-dependent rescue concern, and revert history support the decision without relying on the missing one-day P&L analysis. |
| 438 — depth 60 is a near-perfect availability detector | **ABSENT** for the quoted cross-tab. Current code/logging survives. | Do not loosen the depth gate. | **FAILS as justification for the current live rule.** The initial live gate is 39 contracts, while the gate log says 60; top-ups separately use 60. Fill quality against the actual book is adverse, not the claimed execution asset. The row conflates zero depth, initial sizing, top-up sizing, and candle-vs-book comparisons (R3). |
| 439 — 88–89c YES-only is the survivor | **ABSENT.** The canonical harness cannot reproduce this side-specific separate-book experiment. | Promote the lead, originally as a separate book and later as v5.17. | **UNREPRODUCIBLE / STALE.** Old slippage, rounded history, and a confidence interval touching zero do not support deployment. Current symmetric R2 sweeps are not a substitute for the claimed experiment. |
| 440 — ask 94 separate book killed by executability | **ABSENT.** | Keep 94c outside the live band. | **UNREPRODUCIBLE but conservative.** No present live widening depends on it; it cannot be cited quantitatively. |
| 441 — weather is closed by three methods | **AMBIGUOUS.** One tracked 94KB script and several newer 142–178KB Downloads copies exist; no durable mapping identifies which versions/inputs produced all three tests. | Exclude weather. | **UNREPRODUCIBLE as stated.** The conservative non-inclusion remains, but the three headline results do not share identifiable committed provenance (R1). |
| 442 — only continuous short-deadline markets can host the strategy | **NONE; conceptual generalization.** | Restrict market search. | **PARTIAL heuristic, NOT a proved exclusion.** Finite weather/market tests cannot establish that event markets “cannot” host the pattern at any liquidity. Appropriate for prioritization, not a permanent gate. |
| 443 — doubling P&L is a slots decision | **ABSENT** for the search census; current sweep exists. | Treat more slots as a risk decision. | **PARTIAL.** R2 confirms worse per-trade value at more slots and non-monotone total dollars; it does not prove that no other search result or venue can add independent edge. |
| 444 — Silver lead, Gold bad | Original producer **ABSENT**; current archive/harness can partially rescore series. | Keep Silver in observation until its horizon; exclude Gold. | **PARTIAL / STALE.** The series separation is testable, but the quoted values use rounded history and obsolete slippage. “Do not deploy yet” remains conservative; the exact horizon and economics do not. |
| 445 — searchable universe is 76 series | **ABSENT** and API-state dependent. | Stop broad search outside high-frequency liquid series. | **UNREPRODUCIBLE / PERISHABLE.** Endpoint-field warning is technically checkable, but the universe counts and exhaustion inference need a dated API dump or command output. |
| 446 — fees belong inside break-even | Original dashboard incident producer **ABSENT**; fee formula survives in code and verification. | Compute performance net of fees. | **HOLDS mechanically; magnitude STALE.** The formula is correct and is the only valid break-even definition. The quoted historical dashboard examples are not independently durable. |
| 447 — flat sizing removed dollar-weighting drag | **ABSENT** for the stated window. Current code exposes flat sizing. | Prefer flat sizing. | **PARTIAL.** The mechanical removal of bet-size weighting is real; the quoted ratio, return, p-value, and interval are not reproducible. It does not establish that the current dollar size is optimal. |
| 448 — edge is fragile; require a long horizon | **ABSENT** for the exact power calculation, though arithmetic is reconstructible. | Do not react to short windows. | **HOLDS in direction / exact threshold PARTIAL.** Current slippage makes fragility stronger. The universal sample bar must account for settlement clustering and the actual alternative, not a bare trade count. |
| 449 — strategy is not decaying by fortnight | `scripts/backtest.py` **PRESENT**. | Do not pause on a short bad window. | **PARTIAL / STALE.** The command survives, but current archive composition, rounding, shadow-series inclusion, stake, and fill cost differ. The historical “latest is best” snapshot is perishable, not a standing guarantee of no decay. |
| 450 — capture rate is disputed | `audit2.py` and `scripts/reconcile.py` **PRESENT**. | Do not quote a single capture number. | **HOLDS.** The two tools use different denominators and have not been reconciled into one measure. This row correctly refuses to turn either into a live decision. |
| 451 — selection costs money; execution is an asset | `scripts/reconcile.py` **PRESENT**, but it compares fills with model candles. | Treat selection/capture as the performance leak. | **FAILS in its execution interpretation; selection result STALE.** Book-at-entry execution is `+0.227c` adverse. Candle comparison mixes selection and staleness and cannot establish fill quality (R3). Reconciliation can still measure model/live population differences for a dated window. |
| 452 — one-day selection-leak story superseded | Producer **ABSENT**; explicitly superseded. | Do not change slot ordering from that day. | **NOT LIVE BASIS.** Correctly retired; no current decision should cite it. |
| 453 — first per-side live read | Original settlement query **ABSENT** as a durable artifact; current API check exists. | Reassess NO-side execution concern. | **STALE.** A one-day descriptive split was never a gate. The later book-at-entry sample answers the execution question more directly and finds no established side difference (R3). |
| 454 — thin-book gate might be too strict | Producer **ABSENT**; explicitly refuted. | None; superseded by row 438. | **NOT LIVE BASIS.** The upper-bound candle calculation must not be resurrected as fillable P&L. |
| 455 — EXEC fields enable distributional fill measurement | Logging code and `scripts/verify.py` **PRESENT**. | Measure fills against recent book by side. | **HOLDS.** The proposed method is the method that exposes the stale fill-quality constant. The waiting horizon is complete; the row should point to the result rather than an open question. |
| 456 — MOM3 live occurrence rate is too low | Original one-day harvest producer **ABSENT**; shadow logging and research scripts survive. | Do not expect a prompt prospective MOM3 result. | **UNREPRODUCIBLE / PERISHABLE.** A one-day arrival rate cannot establish the long-run timeline and does not gate live trades. |
| 457 — cooldowns and size-up-after-loss refuted | `research/loss_cooldown/` **PRESENT**. | Do not add a cooldown or post-loss size-up. | **HOLDS for the tested policies, with stale statistics.** R4 still shows post-loss cooldowns generally reduce total return; the short holdout exception is not stable across durations. The scripts model a loss-triggered cooldown, not the live nine-individual-loss kill switch, so they do not justify that live constant. |
| 458 — daily limit 300 beats 200 | `research/loss_cooldown/steelman.py` **PRESENT**. | Keep the 300-dollar stop. | **FAILS on the current archive / exact threshold UNKNOWN.** R4 no longer reproduces “300 is best”: 300, 400, 600, and no stop are identical because they never fire; 150 wins in-sample but is slightly worse than no stop out-of-sample, and 200 reverses across the split. The script approximates rather than reproduces trader state and still uses stale slippage. This is no evidence to change the live stop, but it is also no longer evidence for 300. |
| 459 — high WR / low P&L was sizing artifact | Original window producer **ABSENT**; historical code/config is in git. | Use flat sizing. | **PARTIAL / STALE.** Variable sizing can mechanically create the distortion, but the exact path and figures are not reproduced. The row also says flat 50 while live is 25. |
| 460 — incentives not worth pursuing | Scripts and README **PRESENT**; the original large JSON inputs were deliberately untracked/removed. | Do not pursue incentives. | **UNREPRODUCIBLE offline / PERISHABLE.** Current endpoint rescans can answer current availability, but cannot reproduce the dated corpus totals without the removed inputs. Conservative non-participation is not endangered. |
| 461 — WTI justification inverted | Original producer **ABSENT**; archive and commit history permit a successor analysis. | Pause WTI; revisit metals/WTI at one standard. | **PARTIAL.** The provenance failure is real: WTI entered on a short result and was later paused after inversion. Exact historical values are not durable. Pausing an unestablished series is conservative. |
| 462 — SOL full-history edge is fine | Original split producer **ABSENT**; present harness can rescore. | Do not exclude SOL on a short losing window. | **PARTIAL / STALE.** The anti-cherry-picking principle holds; the quoted aggregate does not survive unchanged archive/fill assumptions. SOL remains live, so this is a non-exclusion rationale rather than positive proof of edge. |
| 463 — first Gold/Silver read | Original producer **ABSENT**; superseded by row 444. | Keep both out pending data. | **NOT LIVE BASIS / STALE.** It is explicitly an early read and must not be used as an established result. |
| 464 — price edge dies at 95c | `scripts/calibration.py` **PRESENT**. | Cap asks below 95c; motivate lower-band work. | **PARTIAL.** R4 still finds no significant edge at 95–96c, while exact lower-cent values have moved. The script integer-buckets rounded archive data and includes shadow series. It supports excluding 95–96c more strongly than it supports a specific lower bound. |
| 465 — earlier entry is better | `scripts/calibration.py` **PRESENT**. | Retain the entry window / avoid late-only entry. | **PARTIAL.** R4 continues to show the 360–480s bucket strongest, but most adjacent intervals include zero and the script inherits archive/harness limitations. “Earlier” is too broad; the evidence is bucket-specific. |
| 466 — waiting for a better price loses | `scripts/entry_timing.py` **PRESENT**. | Buy qualifying 90–91c signals rather than wait. | **HOLDS in direction / exact values STALE.** R4 still shows most early 90–91c contracts leave the band and late survivors are adversely selected. The script hard-codes a 75-dollar stake, omits fees, and uses rounded history, so quoted dollars are not live units. |
| 467 — survivor re-entry lead | Original producer for the stated split **ABSENT**; shadow logging survives. | Observe only; do not gate. | **UNREPRODUCIBLE, correctly non-live.** The sparse holdout and rule-of-three caveat already prevent action. |
| 468 — BRTI basis versus Coinbase | `scripts/calibration.py` **PRESENT**. | Account for oracle basis in spot-derived research. | **PARTIAL / STALE.** R4 still finds a small positive mean basis, but the mean, dispersion, and tail frequency differ. This affects shadow signals, not the core price/prior gate. |
| 469 — volume no longer constrained | Original counter harvest **ABSENT** and one-day. | Suspect marginal extra trades rather than capture. | **STALE / CONTRADICTED by later capture work.** It cannot justify a live gate or permanently retire capture engineering. |
| 470 — no config sweep established | `scripts/backtest.py` **PRESENT**, original bootstrap outputs not retained. | Keep config unchanged. | **PARTIAL / STALE.** Current R2 rankings differ, measured fill cost doubled, selection is rounded, and the harness includes shadow series. “No change established” is a safe conclusion; the quoted deltas/probabilities are not current evidence. |
| 471 — 15M/hourly parity untested | `research/hourly_crypto/` and `parity_audit.py` **PRESENT**. | Do not trade a parity strategy yet. | **HOLDS as a non-result.** The standalone hourly study does not test synchronized relative value; no positive parity evidence supports deployment. |
| 472 — crash fills are positive EV so far | Original fill/settlement harvest **ABSENT**. | Do not auto-exit crash fills. | **UNREPRODUCIBLE and underpowered.** A dozen conditional outcomes cannot establish the changed-probability trade is positive EV. It also does not prove auto-exit is better, so the safe conclusion is UNKNOWN, not either policy. No active-loss bug is established here. |
| 473 — August 18 was a bad model day, not an unexplained live leak | `scripts/backtest.py` **PRESENT**. | Do not diagnose a trader leak from that day alone. | **HOLDS in direction / dollar magnitude STALE.** R2 reproduces the win rate but current 25-dollar sizing halves the old 50-dollar P&L. The harness limitations remain, yet it still establishes that the modeled population also had an unusually bad day. |
| 474 — adverse three-minute momentum predicts losses | `research/perp_overlay/s1_robustness.py` and inputs **PRESENT**. | Keep MOM3 shadow-only pending prospective evidence. | **HOLDS, magnitudes STALE.** R4 still finds the chosen adverse-momentum buckets negative in both windows and robust to worse slippage; present measured slippage makes, rather than breaks, that directional result. It remains a research lead, not a live gate. |
| 475 — hourly ladders have no standalone edge | Producer and compressed archive **PRESENT**. | Do not add hourly markets to the live book. | **HOLDS, restated at current stake.** R4 finds BTC hourly negative and ETH aggregate positive only because stacked extras carry the result; first-strike entries are negative for both. Every entry collides with an hourly 15M settlement and concurrency is not shared. Current dollars differ because the live stake is 25, not 50. |
| 476 — C1 quarantine is harmless noise | `scripts/backtest.py` **PRESENT but mismatched to trader**. | Leave C1 in place. | **FAILS as a proof of the live rule.** The harness evaluates YES-only while live quarantines both sides. Correct live-rule removal is `-$52`, CI crossing zero; verdict remains “unjustified/harmless not established,” not the documented figure (R3). |
| 477 — minimum ask 89 may beat 90 | `scripts/backtest.py` **PRESENT**. | Research a lower bound. | **PARTIAL lead only.** R2 at measured slippage makes 89 slightly higher total dollars but lower per-trade value than 90; rounded selection and symmetric sides prevent it from proving the deployed YES-only rule. |
| 478 — ET08 hour loses | Producer **ABSENT**. | None; wait for more data. | **UNREPRODUCIBLE / NOT LIVE BASIS.** |
| 479 — BNB borderline | Producer **ABSENT**. | Do not exclude BNB. | **UNREPRODUCIBLE.** Non-exclusion on weak evidence is conservative, but the quoted result is not durable. |
| 480 — Thursday blackout lead | Producer **ABSENT**. | Do not add a weekday gate yet. | **UNREPRODUCIBLE, correctly non-live.** |
| 481 — C5 lead underpowered | Producer **ABSENT**; shadow logging survives. | Do not block C5. | **UNREPRODUCIBLE, correctly non-live.** |
| 482 — raise stake at balance 2200 | `research/top5/sizing_validate.py` is a later sizing producer, not the original threshold source. | Keep current stake until balance threshold. | **UNREPRODUCIBLE / STALE.** R4 finds timing-weighted sizing intervals cross zero and can lift cluster exposure above the flat cap. It does not justify a 2,200-dollar threshold or a 100-dollar stake; current live stake is 25. |
| 483 — consecutive loss must group by expiry window | No numerical producer; implementation is inspectable. | Define correlated-loss stopping correctly. | **CLAIM HOLDS conceptually, but current live rule does not implement it.** The trader counts individual losing positions, so simultaneous losses from one expiry can advance the counter multiple times. No evidence establishes the live threshold or its economics; classify the constant UNKNOWN/PARTIAL rather than “justified.” |

## §7 crash-fill prose

| Claim | Producer | Verdict |
|---|---|---|
| A below-band average fill can be real without violating the order's limit ceiling. | Trader/order semantics and fee formula **PRESENT**. | **HOLDS mechanically.** A buy limit is a maximum; price improvement below it is possible. Fee arithmetic can corroborate the average price. |
| Fills within three cents are benign and need no alert. | Original sample **ABSENT**. | **UNREPRODUCIBLE / UNDERPOWERED.** “Within three” is an operational tolerance, not an evidence-established safety boundary. |
| Deeper fills are a materially different risk and should alert. | Code path **PRESENT**; outcome proof absent. | **HOLDS as risk classification.** The payoff and implied probability change materially; alerting is conservative. Old 75-dollar risk examples are stale under the current stake. |
| Count via workflow logs and recover alert history from Gmail. | Workflow/log procedure **PRESENT**; Gmail was not needed for this audit. | **PARTIAL operational recipe.** The run-at-close lookup detail is reproducible; email history is an external dependency, not evidence retained in the repository. |

## §8 claim-family audit

| Claim family | Surviving producer/input | Verdict for current decisions |
|---|---|---|
| Calibration warning: none of §8 is reproducible in the repository | Several later producers now exist: `scripts/vol_bucket_test.py`, `research/perp_overlay/`, `research/hourly_crypto/`, `scripts/xlist_arb.py`, and `backtest_longshot.py`. | **FAILS as a current blanket statement.** It remains accurate only for the untracked/absent families. Each claim must be classified individually, as below. |
| Volatility / dispersion filter is refuted | `scripts/vol_bucket_test.py` **PRESENT**. | **HOLDS, numbers STALE.** R4 still has high-minus-low cluster intervals crossing zero and high dispersion providing most profit as well as most volume. The documented holdout-profit share changed, and the script uses rounded integer prices and a stale hard-coded stake. No volatility veto is supported. |
| Perp hedging fails even before realistic fees; only MOM3 survives | Full `research/perp_overlay/` scripts and cached inputs **PRESENT**. | **HOLDS in the tested design space; magnitudes STALE.** R4 confirms MOM3 robustness. The hedging scripts document zero-fee comparisons, delta sizing, netting, and OOS tests. This does not prove every possible hedge is bad, but it supports not deploying the tested overlays. |
| Directional / entry variants all belong in “not pursued” | `backtest_longshot.py` is tracked. Acceleration, breakout, cross-asset, series-pause, and ultralate scripts exist only as untracked files (with several untracked CSV outputs); exact producers for dislocation scalp, oracle lag, and the ETH-exclusion dollar claim were not found. | **MIXED / MOSTLY UNREPRODUCIBLE.** The aggregate sentence cannot be cited as a verified sweep. Untracked scratch files show work occurred but are not durable provenance. It supports no live exclusion beyond individually reproduced results. Do not treat the ETH dollar figure as durable evidence. |
| Direction-neutral structures were all negative after fees | Exact producers/inputs for the listed structures were not found in the tracked repository surface. | **UNREPRODUCIBLE.** Fee drag is real, but it does not reproduce eleven separate negative experiments. The category is a research-history pointer, not proof that the entire class is closed. |
| Other markets are rejected/not pursued | Weather has ambiguous versions; no complete committed producer set was found for sports, sportsbook, Fed, or the named market scans. | **UNREPRODUCIBLE / EXTERNAL CONSTRAINT PARTIAL.** US account eligibility may rule out venues operationally, but quantitative market verdicts are not durable. Gold/Silver/WTI are addressed individually in §7. |
| Cross-listing three-leg arb is real but economically worthless | `scripts/xlist_arb.py` **PRESENT**; the historical opportunity snapshot is not retained. | **PARTIAL / PERISHABLE.** The scanner can test today's book but cannot reproduce the historical frequency and total. Non-deployment remains conservative because the claimed implementation burden exceeds an unestablished return. |

## UNKNOWN items to carry forward

- The true v5.17 side-specific backtest population, cluster interval, and prospective start date are **UNKNOWN** because the original producer is absent and the deployed path never admitted the intended entries.
- The economic value of the live C1 rule is **UNKNOWN**; the corrected interval crosses zero and the harness/trader mismatch remains.
- The correct initial depth threshold and the value of the separate top-up depth threshold are **UNKNOWN**. Row 438 does not test the current split behavior.
- The expected value of crash fills and the optimal response to a deep fill are **UNKNOWN**. Alerting is justified; hold-versus-exit is not.
- The economics of the live nine-loss counter are **UNKNOWN**. Existing cooldown research tests a different trigger, and the implementation counts correlated individual positions rather than expiry clusters.
- Any exact correction from two exact-cent archive days to the older rounded archive is **UNKNOWN**. Direction is established as optimistic; a precise historical correction is not.
- Quantitative §8 claims without a committed producer/input remain **UNKNOWN**, even when the resulting choice is conservative.

## Bottom line

The research record is materially better than “none reproducible,” but it is not strong enough to support most of the exact figures in §§7–8. Surviving evidence robustly supports: fees inside break-even, no volatility veto, no tested perp hedge, no standalone hourly expansion, no reactive short-window changes, and keeping several exploratory signals shadow-only. It does **not** support the deployed v5.17 band, the current C1 rule, the depth-60 narrative as a description of the live initial gate, or precise claims built on `+0.105c` fills and rounded selection.
