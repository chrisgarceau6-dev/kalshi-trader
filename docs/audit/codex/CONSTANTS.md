# Live-constant evidence audit

Snapshot: 2026-08-24. A commit message or code comment is treated as a claim, not proof. `SUPPORTED` means the stated evidence bears on the chosen value; `PARTIAL` means it supports the direction but not the exact cutoff; `STALE` means later code/data/config undermines applicability; `CONTRADICTED` means later evidence reverses it; `NONE` means no value-specific evidence was found; `DEFECT` means the implementation does not enact the stated constant.

## Reproduction commands

Run from the repository root:

```bash
# Values and every use site.
rg -n '^[A-Z][A-Z0-9_]*(?::[^=]+)?\s*=|MIN_ASK_CENTS <= best_offer|fresh_ask <= 91|ext_priors\[2\] < 80|prior_asks\[1\]|retry_depth < MIN_BOOK_DEPTH|age < 3600|Decimal\("0.9"\)|Decimal\("0.01"\)' late_certainty_trader.py
nl -ba .github/workflows/late_certainty.yml
nl -ba kalshi_auth.py | sed -n '24,156p'

# First/last history for any value or name; substitute NAME or exact assignment.
git log --all --date=iso-strict --format='%H%n%ad%n%s%n%b' -S 'NAME' -- late_certainty_trader.py .github/workflows/late_certainty.yml CLAUDE.md
git blame --date=short -L 104,375 late_certainty_trader.py

# Important evidence-bearing commits.
for c in 0d31b721 5d6fbf4b a3597bbb 31e8198d f06678fe 97991104 \
  78a50aa0 48215ce8 360f34d5 19a7a8d8 32552d61 215d3bc3 \
  47cad226 4b13e845 67e58c77 66765463 e459b0bc b444654f \
  584d0a85 35a9c18b; do
  git show -s --date=iso-strict --format='%H%n%ad%n%s%n%b' "$c"
done

# Does the canonical harness represent the new side-specific constant?
rg -n 'want =|LOW_BAND_MIN|MIN_ASK_CENTS|def qualifies|cfg\["min_ask"\]' scripts/backtest.py
python3 scripts/backtest.py --compare c1=0

# Current execution cost, rounding effect, and known trader/harness gate mismatches.
PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify.py --check slippage
PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify.py --check rounding gates harness

# Reproduce C1 as the trader actually applies it: both sides and int-truncated p2.
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import scripts.backtest as B
live = {'KXBTC15M','KXETH15M','KXSOL15M','KXDOGE15M','KXBNB15M','KXXRP15M'}
rows = [r for r in B.load() if r[0] in live]
cfg = B.live_config()
def qualifies(c,se,side,ask,secs,p1,p2,p3):
    if not(c['min_ask'] <= ask <= c['max_ask'] and
           c['min_secs'] <= secs <= c['max_secs']): return False
    if any(p < c['prior_min'] for p in [p1,p2,p3][:c['lookback']]): return False
    if c['p3_gate'] and ask <= 91 and p3 < c['p3_min']: return False
    if side == 'no' and c['yes_only']: return False
    return not(c['c1'] and se == 'KXSOL15M' and 75 <= int(p2) <= 79)
B.qualifies = qualifies
base_pc, base_tr = B.simulate(rows, cfg, .105)
var_pc, var_tr = B.simulate(rows, dict(cfg, c1=0), .105)
print('base trades', len(base_tr))
print('removing C1 delta, CI, P',
      B.bootstrap(sorted(set(base_pc) | set(var_pc)), base_pc, var_pc))
PY

# Identify which archive days contain any genuinely fractional-cent gate input.
python3 - <<'PY'
import csv,gzip,glob,os
for f in sorted(glob.glob('data/candles/*.csv.gz')):
    with gzip.open(f,'rt') as z:
        rows=list(csv.DictReader(z))
    n=sum(any(v and float(v) != int(float(v))
              for v in (r['ask'],r['prior_1'],r['prior_2'],r['prior_3']))
          for r in rows)
    if n: print(os.path.basename(f), n)
PY
```

The final command prints only the 2026-08-22 and 2026-08-23 files. Every earlier retained day stores integer-cent gate inputs. Parsing those values as floats is technically valid, but it cannot recover the lost fractional quote or establish true band membership.

## Money-path strategy constants

| constant / gate | live value | stated justification | justification date | VERDICT |
|---|---|---|---|---|
| `SERIES_LIST` | BTC, ETH, SOL, DOGE, BNB, XRP fifteen-minute series | Current code comments call these the OOS-validated crypto set. WTI's addition and later pause demonstrate a same-mechanism series can reverse when its life history grows (`7272d0cc`, then `35a9c18b`). | 2026-08-19 latest relevant review | **PARTIAL.** The six names are explicit, but no single current command applies one admission standard to every included and excluded series. |
| WTI exclusion | absent live; present in shadow | Added on a thirteen-day result of `+$1.75/trade`; whole-life result later read `-$0.33/trade` over two hundred ninety trades, so it was paused pending about one thousand observations alongside metals (`35a9c18b`). | 2026-08-19 | **SUPPORTED as a pause.** The evidence explicitly inverted and the commit avoids claiming proof of negative edge. |
| `STRATEGY_VERSION` | `v5.17` | Version label for the YES-only lower-band change (`584d0a85`). | 2026-08-24 | **DEFECT.** The label claims a configuration the mandatory last-look does not execute. |
| `MIN_ASK_CENTS` | `90¢` | Max-return search in `0d31b721`: a widened `90–99¢` range with different prior rules beat v4 in a sixty-day/OOS analysis. | 2026-08-01 | **STALE.** The current upper bound, priors, sides, slots, stake, and data representation differ; historical quotes were rounded. On exact-cent days, rounding changes 40.4% of selected identities and is optimistic. The old result supports “lower than 95,” not this exact current floor. |
| `MAX_ASK_CENTS` | `93¢` | Code comment/subject says lowering `95→93¢` avoids partial fills in the thin `94–95¢` book (`5d6fbf4b`). | 2026-08-09 | **PARTIAL.** A mechanism is stated, but the commit has no sample, command, or cutoff comparison. Later research finds some higher-price signals profitable but often unexecutable. |
| `LOW_BAND_MIN_CENTS` | intended YES floor `88¢` | `584d0a85` cites full-archive results at modeled slippage, side asymmetry, bootstrap interval/probability, and a fixed prospective decision rule. | 2026-08-24 | **DEFECT / UNREPRODUCIBLE.** `scripts/backtest.py` does not read this constant and applies a symmetric floor. The four tests exercise helpers, not the entry path. The downstream mandatory book gate rejects `88–89¢` YES. |
| `YES_ONLY` | `False` | `31e8198d` reports full retained history, cluster bootstrap, holdout sign flip, base-rate parity, and doubled-book economics. | 2026-08-17 | **STALE/PARTIAL.** The statistical argument addresses side symmetry, but most of the retained history had rounded prices and the current asymmetric YES band is not represented by the canonical harness. |
| `PRIOR_MIN_CENTS` | `75¢` | `a3597bbb` says relaxing `80→75¢` plus shorter lookback yields eighty-nine percent more volume; inline comments split that into volume claims. | 2026-08-10 | **STALE.** The commit has no body or reproducible command, its audit script rounds prices, and the pre-2026-08-22 archive cannot recover exact boundary membership. The later selected-trade comparison finds 40.4% identity disagreement under rounding, always optimistic. |
| `PRIOR_LOOKBACK` | two candles | Same filter audit as the prior minimum. | 2026-08-10 | **STALE** for the same reasons; joint changes do not isolate this exact choice, and the later selected-trade rounding comparison is much larger than the row-level estimate. |
| low-ask gate | fresh ask `≤91¢` requires third prior `≥80¢` | `47cad226` reports prior-two-only below break-even and prior-three-positive on a cross-tab. | 2026-08-15 | **STALE/PARTIAL.** Direction is evidence-based, but there is no commit command and the source quotes around the threshold were rounded. It now also governs the unmodeled YES lower band. |
| C1 SOL quarantine | live trader skips both YES and NO when integer-truncated second prior is `75–79¢` | `4b13e845` code comment cites adverse in-sample and OOS subsets. Current CLAUDE later says the original result was noise and supplies `--compare c1=0`. | 2026-08-16 original; 2026-08-24 rerun | **STALE / UNRESOLVED.** `scripts/backtest.py --compare c1=0` measures a different YES-only rule because its comment falsely claims that matches live. Reproducing the trader's both-side, integer-truncated rule gives a `-$52` removal delta with 98.75% interval `[-$310,+$191]`; nothing is established. |
| `MIN_SECS_LEFT` | `150s` | `f06678fe` links the buffer to three actual final-minute crash fills and order latency; `78a50aa0` later rejects raising it to `240s` because the relevant interval spans harm and benefit. | 2026-08-11 latest | **SUPPORTED for retaining 150 over 240; PARTIAL as an optimum.** |
| `MAX_SECS_LEFT` | `600s` | `48215ce8` keeps the `600–700s` idea shadow-only while removing unsupported filters. | 2026-08-12 | **PARTIAL.** It supports not promoting 700 without prospective evidence; it does not prove 600 is optimal. |
| `BLACKOUT_HOURS` | empty set | `360f34d5` removes ET13 after a multiple-comparison/noise diagnosis; earlier UTC-hour filters were also removed for lack of mechanism or lost P&L. | 2026-08-16 | **SUPPORTED.** The evidence argues against data-mined hourly exclusions. |
| `FLAT_BET_DOLLARS` | `$25` | `32552d61` is explicitly a survival decision: more loss headroom above the cash stop with the same statistical learning rate, and a predeclared restoration condition. | 2026-08-22 | **SUPPORTED conditionally.** The rationale is coherent for the balance at that date, but it becomes stale as balance/headroom changes; the restoration conditions require current measurement. |
| `LIMIT_BUFFER` | `2¢` | `f06678fe` traces three real losses to aggressive limits and replaces the broad ceiling with ask plus two cents. | 2026-07-31 | **SUPPORTED direction; PARTIAL exact value.** Tight limits address the observed mechanism; no comparison establishes precisely two cents. |
| `LISTING_QUOTE_TOLERANCE` | `3¢` | `67e58c77` reports ninety-six listing/fresh comparisons, median disagreement, and band disagreements; fresh quote still enforces the true band. | 2026-08-22 | **SUPPORTED/PARTIAL.** Evidence supports a widened prefilter and a three-cent tail cover, but it is a small convenience sample. |
| book requirement, first attempt | `max(ceil(contracts×1.5),25)`; thirty-nine contracts at live max limit | `66765463` shows the old sixty-contract rule was calibrated at a larger stake and blocked a fillable live-sized BNB order; ratio tracks order size. | 2026-08-22 | **SUPPORTED direction; PARTIAL exact ratio/floor.** Scaling is logically necessary, but the fifty-percent buffer and floor are policy values, not estimated optima. |
| `MIN_BOOK_DEPTH` on retries | `60` contracts | Retained as “legacy”; the same `66765463` evidence says a fixed sixty became improperly strict at the current stake. | 2026-08-22 latest analysis | **CONTRADICTED.** Top-up code still uses the stale constant, so retry behavior conflicts with the adopted dynamic-depth rationale. |
| `MAX_CONCURRENT_POSITIONS` | `2` | `215d3bc3` restored two at a `$75` stake to match an old exposure ratio. `584d0a85` later says three slots worsens per-trade economics for the new band. | 2026-08-24 latest claim | **STALE/PARTIAL.** The original dollar-risk calculation is stale at `$25`; the new-band comparison is not reproducible with the canonical harness because it cannot model the new band. Its `0.105¢` execution assumption is also stale: the book-to-fill measurement is `0.227¢` adverse. Two remains conservative. |
| candidate priority | most seconds left, then MD5 cluster/series | inline code cites live/model reconciliation and fixed-series concentration; commit `80d52b44` introduced allocation by time. | 2026-08-22 | **PARTIAL.** Determinism/diversification are supported, but MD5 is a neutral tie-break policy rather than an evidence-derived optimum. |

## Risk and execution constants

| constant / gate | live value | stated justification | date | VERDICT |
|---|---|---|---|---|
| daily loss formula | `max($300, bet×4)` = `$300` now | `19a7a8d8` says `$300` was evaluated on retained history and a silently created `$200` threshold had never been tested; later CLAUDE says a sweep favored `$300`. | 2026-08-19; later note 2026-08-21 | **STALE / UNRESOLVED.** Current `python3 research/loss_cooldown/steelman.py` no longer reproduces “$300 is best”: `$300`, `$400`, `$600`, and no stop are identical because none fires; `$150` is best in-sample but slightly worse than no stop out-of-sample, while `$200` reverses across the split. The simulator also approximates rather than reproduces live rolling P&L and inherits rounded archives. No exact threshold is established. |
| `ROLLING_PNL_SECONDS` | `86,400` | `3be56b79` prevents midnight reset from granting two adjacent daily budgets. | 2026-08-11 | **SUPPORTED as an accounting window.** Exactly one day is a policy choice, not an edge estimate. |
| `STOP_BALANCE` | `$650` | `8a87e999` set it proportionally to a `$100` stake and post-deposit balance; later code retains the absolute floor after stakes fell. | 2026-08-14 | **STALE.** The proportional rationale no longer matches the `$25` stake or current balance. No later value-specific revalidation was found. |
| `CONSEC_LOSS_LIMIT` | nine | `360f34d5` says five fired frequently on correlated clusters while nine never fired over the tested history. | 2026-08-16 | **PARTIAL.** It removes false alarms, but “never fires” does not show it detects genuine degradation. The one-hour cooldown itself dates to initial code and has no value-specific evidence. |
| edge window / threshold | fifty outcomes / `84%` | threshold commit `71e471a6` says lowering eighty-eight percent and adding recovery fixes deadlock; inline comment calls eighty-eight a two-sigma false trigger. The fifty-row window predates that change. | 2026-08-11 threshold; 2026-08-04 window | **NONE/PARTIAL.** No command, distribution, power calculation, or evidence for precisely fifty/eighty-four percent was found. |
| edge cooldown/recovery | `7,200s`, clear only with fewer than three consecutive losses | `71e471a6` frames this as deadlock prevention. | 2026-08-11 | **PARTIAL.** Auto-recovery has an operational rationale; the exact duration and three-loss rule have no supplied calibration. |
| `ORDER_TTL_SECONDS` | four | `51137594` adds server-enforced expiry while hardening execution. | 2026-08-14 | **NONE for the exact value.** Comment explains the guard, but no latency/fill distribution justifies four seconds. |
| `ORDER_RECONCILE_SECONDS` | eight | same hardening commit | 2026-08-14 | **NONE for the exact value.** No propagation-latency evidence was found. |
| `ORDER_FILL_WAIT_SECONDS` | three | `215d3bc3` restores three from one to preserve queue priority/liquidity pending live data. | 2026-08-15 | **PARTIAL / explicitly unvalidated.** The commit itself says live data is pending. |
| `ORDER_MAX_ATTEMPTS` | three | `6e4f3d49` introduces bounded fresh-validated top-ups. | 2026-08-14 | **PARTIAL.** Bounded retries address partial fills; exactly three is not supported by a comparison. |
| `ORDER_MIN_TOPUP_DOLLARS` | `$5` | same commit; comment says avoid dust orders. | 2026-08-14 | **NONE for the exact cutoff.** |
| `CRASH_FILL_TOLERANCE` | `3¢` | `97991104` classifies twelve sub-band fills; six were described as shallow, with deep fills alerting. | 2026-08-18 | **PARTIAL.** It supports tiered alerts, but the small sample does not establish exactly three cents. |
| mandatory last-look band | hardcoded `90–93¢` both sides | `97991104` added it after deep crash fills. | 2026-08-18 | **SUPPORTED for a final book gate; DEFECT at the lower bound.** It was not updated for the side-specific v5.17 floor and blocks the new strategy. |
| fill outside-band floor | hardcoded `90¢` both sides | inherited from the same crash-fill logic | 2026-08-18 | **DEFECT.** Valid intended `88–89¢` YES fills are mislabeled and top-ups stop. |
| partial-fill log | below ninety percent of requested contracts | `6e4f3d49` | 2026-08-14 | **NONE.** Logging only; no exact threshold evidence found. |
| principal tripwire | target plus `$0.01` | `6e4f3d49` | 2026-08-14 | **SUPPORTED as a rounding tolerance;** not an edge parameter. |
| state cap | five hundred settled positions | initial operational constant `8271ab8a`; no rationale beyond file compactness | 2026-08-04 | **NONE for the exact cap.** It does not remove unsettled exposure. |
| ambiguous POST lookup | three pages × two hundred orders | comments require distinguishing absent from unknown | current code | **PARTIAL.** Three-valued handling is sound; the bounded history depth is not justified against order volume. |
| positions/orders/fills API limits | two hundred per page / one thousand fills | `3be56b79` raised fills from fifty after exact-fee work; other limits follow pagination | 2026-08-11 latest fills change | **SUPPORTED operationally** because positions/orders exhaust cursors and fills are order-filtered; the fills endpoint itself is not paged, so completeness still depends on server filtering/limit behavior. |
| V2 order semantics | limit; GTC; four-decimal fixed price; YES=`bid`, NO=`ask`; `taker_at_cross`; cancel on pause | `kalshi_auth.py` implementation and current official API schema | current code/docs | **SUPPORTED** by official [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2), [Cancel Order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2), and [Get Order](https://docs.kalshi.com/api-reference/orders/get-order). |
| signed HTTP timeout | twenty seconds | initial auth helper; no code comment or commit rationale | 2026-07-31 | **NONE for the exact timeout.** |

## Workflow and live-process constants

| constant | live value | justification | date | VERDICT |
|---|---|---|---|---|
| daemon interval | fifteen seconds | `12c78ecf` diagnoses low capture from transient one-minute signals and measures a large capture gap under sparse CI scans. | 2026-08-17 | **SUPPORTED direction; PARTIAL exact cadence.** |
| daemon duration | nine hundred seconds | `b444654f` measures fixed runner overhead, blind gaps, and duty cycle, concluding longer jobs improve coverage and cost. | 2026-08-22 | **SUPPORTED.** |
| job timeout | twenty minutes | same commit caps a hung job slightly beyond the fifteen-minute scan window. | 2026-08-22 | **SUPPORTED operationally.** |
| follow-up delay | five seconds | `12c78ecf` drops the former thirty-second sleep because the daemon now supplies spacing. | 2026-08-17 | **SUPPORTED direction;** exact five seconds is not calibrated. |
| two staggered cron schedules | every five minutes at offsets zero and two | workflow comment says backup trigger bounds gaps; concurrency serializes jobs. | 2026-08-06 | **STALE/PARTIAL.** Fifteen-minute self-dispatched jobs make these primarily recovery triggers; recent scheduled runs can queue/cancel rather than set actual scan cadence. |
| workflow concurrency | one `certainty-trader` group; do not cancel in progress | prevents overlapping live traders/double entries | 2026-08-04 | **SUPPORTED as a safety invariant.** |
| daemon failure limits | ten consecutive or final share strictly above `0.34` | `e459b0bc` defines and tests healthy, isolated, alternating, and systemic patterns. | 2026-08-22 | **SUPPORTED for the stated failure model.** |
| state artifact/cache | artifact retention one day; cache key per run with prefix restore | workflow configuration; state continuity is required for exposure/stats | 2026-08-08 latest retention | **PARTIAL.** Continuity need is real; exact retention and cache semantics are operational choices, and cache restore is prefix-based rather than a transactional database. |

## Shadow-only constants

These execute in the live process but cannot authorize an order. They can still affect scan latency, so they are included.

| group | value | justification/date | VERDICT |
|---|---|---|---|
| `SHADOW_SERIES` | HYPE15M, BTC/ETH hourly, WTI hourly, WTI/GOLD/SILVER fifteen-minute | comments retain candidates until adequate same-standard evidence; latest set change `b9099506`, 2026-08-19 | **SUPPORTED as observation, not inclusion evidence.** |
| survivor windows | now `94–96¢` at `150–240s`; earlier `92–93¢` at `480–600s` | code comment says holdout has only forty-one observations and requires about five hundred; `7b7feec7`, 2026-08-18 | **SUPPORTED as shadow-only.** The code correctly refuses to promote underpowered evidence. |
| momentum | three-minute move, sixty-return volatility window | pre-registered cross-split result cited in code; `d22512e5`, 2026-08-20 | **SUPPORTED as shadow-only;** live incidence was later much lower than forecast. |
| gate-log union | ask `88–99¢`, seconds `150–900`, one hundred eighty candle fetches/process | captures every historical band; `3b5eb093` sizes ceiling to the theoretical workflow maximum, 2026-08-22 | **SUPPORTED operationally.** The budget affects telemetry completeness, not orders. |
| future `600–700s` scan | log only | `48215ce8` explicitly reserves it for OOS validation, 2026-08-12 | **SUPPORTED as shadow-only.** |
| dead longshot constants | asks `15–19¢`, priors, `300–900s`, `$5` | historical trial code | **NOT LIVE.** Neither longshot discovery nor trade function has a caller; these values do not belong in the effective strategy. |

## Bottom line

- **Confirmed implementation defect:** the v5.17 `88–89¢` YES extension is blocked by the mandatory hardcoded `90¢` book floor and misclassified by fill logic. Production logs reproduce it.
- **Confirmed stale implementation:** top-up depth still requires the legacy fixed sixty contracts after the first attempt moved to dynamic depth.
- **Evidence not reproducible as claimed:** the canonical harness labels itself v5.17 but ignores `LOW_BAND_MIN_CENTS`; it simulates a symmetric `90–93¢` book.
- **Historically contaminated constants:** `MIN_ASK_CENTS`, `PRIOR_MIN_CENTS`, `PRIOR_LOOKBACK`, price-bucket work, and other pre-2026-08-22 boundary claims were derived from integer-rounded archives unless independently rerun from raw exact quotes. On the two exact-cent days, true-versus-rounded simulation changes 40.4% of selected trade identities and biases trade count, win rate, and per-trade edge upward; the older row-level 4.1% estimate materially understated decision impact.
- **Stale execution assumption:** every “at measured fill quality” result using `--slip 0.105` is optimistic. `python3 scripts/verify.py --check slippage` measures `+0.227¢` adverse against `book_at_entry` on five hundred fills (`t=+6.6` versus the documented value). This reduces modeled edge magnitude without inverting the decisions checked in Session 2.
- **UNKNOWN rather than silently accepted:** exact justifications for the edge breaker, stop balance at current sizing, TTL/reconciliation durations, retry count, dust cutoff, state cap, and several API/alert timeouts do not exist in the inspected evidence.
