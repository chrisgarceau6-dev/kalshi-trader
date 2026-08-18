# Kalshi Trading — Auto-Context

Claude Code reads this file automatically when opened in `/Users/chrisgarceau/pm/`.

**How to read this file.** It is organised by how fast things rot. Invariants are
structural and safe to rely on. Config is current and verifiable. Dated observations
are perishable. The kill-list at the bottom is *the least reliable section in this
file* — treat it as leads, not verdicts.

## Who / What

- Chris Garceau — UMass freshman. Real money on Kalshi. Wants concise responses, no
  encouragement without evidence, no emojis. Lead with numbers, then takeaways.
- Repo: `/Users/chrisgarceau/pm/` → GitHub `chrisgarceau6-dev/kalshi-trader` (PUBLIC)
  Renamed from `polymarket-monitor2` on 2026-08-18. **The Render service kept the old
  name**, so the dashboard URL is still `polymarket-monitor2.onrender.com` — that
  mismatch is expected, not stale. Renaming the service would change the URL with no
  redirect. Workflow self-dispatch uses `$GITHUB_REPOSITORY`, so it survives renames.
- Live entrypoint: `late_certainty_trader.py` on `origin/main`, workflow
  `.github/workflows/late_certainty.yml`
- **Primary trigger:** self-dispatch — each run sleeps ~30s then dispatches itself via
  `GH_DISPATCH_TOKEN`. **Backup cron:** `*/5` and `2-59/5` staggered.

---

# 1. Check claims, don't trust them

**Every strategy claim in this file has a command. Run it.**

```bash
python3 scripts/backtest.py                       # live config, full history
python3 scripts/backtest.py --compare yes_only=1  # baseline vs variant + bootstrap CI
python3 scripts/backtest.py --sweep max_ask 92 93 94 95 96
python3 scripts/backtest.py --slip 1              # one tick of adverse fill
python3 scripts/backtest.py --since 2026-08-13    # holdout window
```

Defaults are read out of `late_certainty_trader.py` by AST, so the harness cannot
drift from what is actually running. Data is `data/candles/*.csv.gz`.

**Why this matters.** This file used to record conclusions as prose with numbers
attached — "NO side is -EV", "two NOs per cluster is -$1,211", "C1 is -$7.25/trade".
None could be re-run, so none could be challenged, and on 2026-08-17 all three turned
out to be wrong or badly overstated after months of steering decisions. **A number
with no command behind it is an assertion, not evidence.** If you cannot reproduce a
figure with the harness, treat it as unverified no matter who wrote it down.

---

# 2. Invariants — these do not rot

1. **You risk $75 to win ~$6.50.** Break-even is ~92% WR (90.6% at 90¢ → 93.5% at
   93¢). You run ~94%. **The entire edge is that ~2pp gap**, harvested over thousands
   of bets. No single trade is good.
2. **One cent ≈ half your edge.** A 1¢ move in entry price shifts break-even by ~1pp.
   One tick removes ~69% of all profit (`--slip 1`). Execution quality dominates
   every strategy parameter. Guard it; it is already near-optimal so there is little
   to gain, and a lot to lose.
3. **The 7 series settle simultaneously.** They are one correlated bet, not seven.
   Any statistic must resample by *close cluster*, never by trade — the harness does
   this. Per-trade CIs are wrong here and will overstate significance badly.
4. **Kalshi retains settled markets ~67 days.** This is the binding constraint on all
   validation. See §6.
5. **These markets are fair coin flips.** 49.8% settle YES (n=27,908); spot is above
   the strike in 49.5% of markets. Neither side is structurally favoured, and there is
   no drift or strike-placement effect to exploit.
6. **Signals are transient — the backtest sees more of them than the bot can.**
   70% of qualifying signals are in-band for exactly ONE 1-min candle (median 1 of
   ~8 possible). The ask passes *through* 90-93¢ on its way to 100¢ as certainty
   resolves; it does not rest there. So **every backtest figure is an upper bound**
   — it evaluates every candle, while the live bot only sees the ask at poll
   instants. Before v5.16 the bot polled every ~48s and captured **27%** of the
   entries the backtest takes (23 of 86 on 2026-08-16). It now polls every ~15s
   inside a 240s job (~14-16 scans/job, ~18s effective cadence).
   **When live P&L runs below backtest, check capture rate before suspecting edge
   decay.** That gap explained a 3x shortfall that had been misread as decay.
   The missed signals are not worse: transient 93.30% WR / +$0.58 per trade vs
   persistent 93.27% / +$0.77.
7. **Kalshi opens a 15M market only ~600s before its close.** `MAX_SECS_LEFT=600`
   is therefore already at the structural ceiling — there is nothing earlier to
   capture, and raising it gains nothing.
8. **Post-hoc filter discovery has a terrible track record here.** H4, near-strike,
   ET13, UTC blackouts, C1, and a "dispersion filter" built on 2026-08-17 all looked
   convincing in-sample; the dispersion filter *inverted* out-of-sample. With ~2pp of
   edge and ~68 days of data, filter-hunting mostly fits noise. **The wins have come
   from removing restrictions, not adding them.**

---

# 3. Current strategy — v5.16 (2026-08-17)

Buy **either side** at ask [90,93]¢ with 150-600s left, provided the prior 2 same-side
1-min candles are all ≥75¢. If ask ≤91¢, also requires a 3rd prior candle ≥80¢. Hold
to settlement.

| Parameter | Value | Re-check |
|---|---|---|
| `FLAT_BET_DOLLARS` | 75 | balance-gated, see §4 |
| `MIN_ASK_CENTS` / `MAX_ASK_CENTS` | 90 / 93 | `--sweep min_ask 88 89 90 91` |
| `MIN_SECS_LEFT` / `MAX_SECS_LEFT` | 150 / 600 | `--sweep min_secs 100 150 200 250` |
| `PRIOR_MIN_CENTS` / `PRIOR_LOOKBACK` | 75 / 2 | `--sweep prior_min 70 75 80 85` |
| `YES_ONLY` | **False** | `--compare yes_only=1` |
| `MAX_CONCURRENT_POSITIONS` | 2 | `--sweep max_conc 1 2 3 4 --slip 0.105` |
| `MIN_BOOK_DEPTH` | 60 | not in harness (needs live book) |
| `STOP_BALANCE` | 650 | — |
| `CONSEC_LOSS_LIMIT` | 9 | never fires in 68d either way |
| poll cadence | 240s job / 15s interval | ~14-16 scans per CI job |
| `EDGE_DEGRADE_THRESHOLD` | 0.84 | catastrophic breaker only |
| `BLACKOUT_HOURS` | `set()` | — |

**Series:** KXBTC15M, KXETH15M, KXSOL15M, KXDOGE15M, KXBNB15M, KXXRP15M, KXWTI15M
(WTI only exists from 2026-08-01 — it is new, not missing.)

**Order flow:** live-position refetch → fresh-ask refetch (~200ms pre-order) →
side-aware book-depth check → GTC limit at `min(93, fresh_ask+2)` → sleep 3 → cancel →
query fills by `order_id`.

---

# 4. Why the config is what it is

**`YES_ONLY = False` (v5.16).** The NO side was suspended on live YES 46W/1L vs NO
74W/8L. That is **not significant**: z=1.64, two-sided p=0.102, on n=47 YES trades —
about five coin flips of luck, worth roughly the entire $210 P&L gap. Over the full
68 days NO wins **93.65%** and YES **93.79%**; the cluster-bootstrapped difference is
+0.75pp [-0.36, +1.86] in-sample and **-1.59pp** [-4.54, +1.35] on holdout — both
include zero and the sign flips. `--compare yes_only=1` reproduces it; the Aug 13-17
holdout independently confirms YES-only is worse (delta -$1,037, CI excludes zero).

**Execution gap = +0.105¢ (measured 2026-08-17).** Actual contract-weighted fill
91.968¢ vs backtest-predicted entry 91.863¢, over 1,486 fills / 19,245 contracts.
Fills are essentially at the quote. **Method matters:** compare *distributions*, not
each fill against a candle — per-fill comparison yields +0.85¢ that is pure artifact,
because the 1-min reference is stale 47% of the time and produces textbook regression
to the mean. Re-measure after NO accumulates settlements; we have **no NO-side fill
data at all**, and thin NO books are the main live risk of v5.16.

**`MAX_CONCURRENT_POSITIONS = 2`.** Caps a settlement cluster at $150 **regardless of
side** — worst observed cluster is -$150 with or without NO. 3-4 backtest better at
perfect fills and clearly worse at one tick, and raise worst-cluster loss to -$225.

**`MAX_ASK_CENTS = 93` is a slippage bet, not a validated edge.** 94-96¢ backtest
higher at quoted fills and worse at one tick: at 96¢ you win 4¢, so a 1¢ slip costs
25% of gross profit versus 10% at 90¢. Widening spends your best asset for a backtest
number. **Do not raise it without re-measuring fill quality first.**

**Position sizing — $75 until balance ≥ $2,200.** Bet size is a risk decision, never a
win-rate decision. Rule: raise only when the new size is ≤4.6% of balance (the ratio
$75 held at $1,631). Block bootstrap over 60d: $75 → 13.3% chance of touching the $650
stop; $100 → 24.6%. $100 buys +$372 of profit for -$386 of extra drawdown. Rejected.
The old "200 settlements ≥93% WR" gate was met at exactly 93.0% but tested the wrong
quantity — break-even is ~92%, so it authorised leverage on a 1pp margin.

**Daily loss limit ($300, rolling 24h) — validated, do NOT loosen.** Fires ~7 days in
68 at full capture. The trades it blocks are genuinely bad: **90.51% WR, -$1.62/trade
over 495 trades**, against ~92% break-even — blocking them is worth **+$802** over 68
days. Mechanism: the 7 series settle together, so a -$300 day is a whipsaw regime and
it persists for hours. Contrast with the polling gap, where missed trades won 93.30%
vs 93.27% for captured ones — that was a genuine leak; this is a filter that works.
More halts at higher volume is the control doing its job.

**All other halts check out too.** `CONSEC_LOSS_LIMIT=9` never fires in 68 days at
either volume. `MAX_CONCURRENT=3` is *worse* than 2 at measured fill quality
(+$108 vs +$112/day) and collapses under one tick (+$6 vs +$29/day) while doubling
per-cluster exposure. Nothing here is too conservative — verify with
`--sweep max_conc 1 2 3 4 --slip 0.105` before revisiting.

---

# 5. Operational

**Key files**

| File | Purpose |
|---|---|
| `late_certainty_trader.py` | THE live trader — surgical edits only (real money) |
| `scripts/backtest.py` | Canonical harness — every claim goes through this |
| `scripts/archive_candles.py` | Nightly archival (§6) |
| `data/candles/YYYY-MM-DD.csv.gz` | Canonical dataset, ~13 KB/day, from 2026-06-11 |
| `daily_summary.py` | Ground-truth P&L from settlements API (never trust state P&L) |
| `kalshi_auth.py` | RSA-PSS-SHA256 auth + `cancel_order()` |
| `kalshi_dashboard.py` | Render dashboard entrypoint |
| `test_order_safety.py` | Order-safety + NO-path regression tests |

**Auth.** Key ID in GitHub Secret `KALSHI_API_KEY_ID`; private key at
`~/.kalshi/private_key.pem` (NOT `pm/kalshi_key.pem`). Signature must include the
`/trade-api/v2` prefix. **CI passes the key as base64 PEM *content* in
`KALSHI_PRIVATE_KEY`, but `load_private_key()` only reads a file path** — any script
running in Actions needs an `_ensure_key()` helper (see `daily_summary.py` /
`scripts/archive_candles.py`) or it fails on every run. The GitHub remote URL contains
a PAT — flagged for rotation.

**Push flow.** Never push to main.
1. `git fetch origin && git checkout -b <branch> origin/main`
2. `python3 -m py_compile late_certainty_trader.py` — must pass
3. `python3 -m pytest test_order_safety.py` — all must pass
4. `git push origin <branch>`
5. `TOKEN=$(git remote get-url origin | sed 's|https://\([^@]*\)@.*|\1|')`
6. `GH_TOKEN=$TOKEN gh pr create ...` then `gh pr merge <n> --squash`
7. **After merging, confirm the next Actions run actually succeeds.** Verifying that
   `main` has the right code checks the wrong thing. On 2026-08-17 a deleted test
   file left a stale reference in `late_certainty.yml`'s pre-flight step; the trader
   failed on every dispatch for **2h46m** and nothing surfaced it.
   Note runs created within ~1 min of the merge may still use the OLD workflow file —
   check a run whose `createdAt` is clearly after `mergedAt`.
8. **Before deleting any file, grep `.github/workflows/` for it.** The workflows
   hardcode filenames.

**Never `git add -A`** — the working tree holds ~730k lines of untracked research
CSVs. Stage explicitly. Resolve `.claude-flow/` conflicts with `git checkout --theirs`.

**Kalshi API**
- Candles: `/series/{s}/markets/{t}/candlesticks`
- Orderbook: `/markets/{t}/orderbook` → `orderbook_fp.no_dollars` (NO bids) and
  `yes_dollars` (YES bids). **A YES buyer lifts NO bids; a NO buyer lifts YES bids.**
  Getting this backwards was a real bug fixed in v5.16.
- Fills: `/portfolio/fills` — `fee_cost` has the exact fee. Note the account has
  non-late-certainty fills on it (pre-v5.6.4 94-99¢ entries, and sub-88¢ crash-through
  fills); filter to 88-93¢ for strategy analysis.
- Settlements `/portfolio/settlements`, positions `/portfolio/positions`,
  deposits `/portfolio/deposits`
- Spot: Coinbase `api.exchange.coinbase.com` (Binance geo-blocked)

**Dashboard.** https://polymarket-monitor2.onrender.com — Render free tier, ~30s cold
start. P&L / Balance toggle, auto-refresh 30s, ranges floor at Aug 1. Env vars are
`KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY` (raw PEM, not base64), plus `DASH_TOKEN`.

**`DASH_TOKEN` is required on Render.** Until 2026-08-18 the dashboard served live
balance, full deposit history and open positions to anyone — unauthenticated, at a URL
published in this public file. Reach it as `/?t=<DASH_TOKEN>` once; it sets a 90-day
cookie. A hosted instance with no `DASH_TOKEN` returns 503 rather than failing open.
Local runs bind to 127.0.0.1 and stay open.

**Trader health pill.** The dashboard reads the Actions API
(`/actions/workflows/late_certainty.yml/runs`, unauthenticated, 90s cache) and shows
last *successful* run plus consecutive-failure count. Staleness alone would not have
caught 2026-08-17 — cron kept creating runs every 5 min while every one failed, so the
failure count is the fast detector. Successful runs land every ~4.2 min (median, n=34),
so a healthy last-success age cycles 4-9 min; amber >15 min, red >25 min or 2+ failures.
`cancelled` is routine (cron collides with self-dispatch) and never counts as a failure.

**The blackout banner is read from the trader by AST**, like `scripts/backtest.py`.
It was hardcoded to `[13]` while the trader ran `BLACKOUT_HOURS = set()` — every day at
1pm ET the dashboard claimed the strategy was paused while it traded through. Never
hardcode a strategy constant into the dashboard.

---

# 6. Data archival — read before proposing any research

**Kalshi retains settled markets only ~67 days.** This one fact blocked every strategy
question this project ever asked: by the time a hypothesis exists, the only data that
exists is the data it was formed on. The NO question sat unresolved for months because
its gate demanded 90 days of clean holdout — on 2026-08-17 the entire available
holdout was **7.6 days**.

`.github/workflows/archive_candles.yml` runs nightly and commits
`data/candles/YYYY-MM-DD.csv.gz` (all series, both sides, ask 88-96¢, 100-800s —
deliberately wider than the live gates). Backfilled to 2026-06-11, no gaps.

**Every archived day is untouched out-of-sample data for every hypothesis formed after
it.** Backfill with `--backfill N`. Never delete this directory. If the workflow has
been failing, fixing it outranks whatever research prompted the check — a day not
archived is validation capacity permanently destroyed.

---

# 7. Dated observations — perishable

Each needs re-checking; none is settled.

| Observation | Date | Status |
|---|---|---|
| C1 quarantine (SOL + prior2 75-79¢) | Aug 17 | Worth **~$260 over 68 days** — noise, not the "-$7.25/tr" originally recorded. Quarantined on 60+80 trades, violating the 500-trade bar. Left in place as harmless; do not cite as precedent. `--compare c1=0` |
| `MIN_ASK = 89` may beat 90 | Aug 17 | Better at *both* slippage levels — the only parameter that didn't flip. Worth real work. `--sweep min_ask 88 89 90 91` |
| ET08 hour | — | -$2.39/tr, unconfirmed across periods. Needs 500+ trades. |
| BNB exclusion | — | 92.2% WR, +$0.06/tr — borderline. |
| Thursday blackout | — | 349 trades at -$1.20/tr — suggestive, underpowered. |
| C5 (prior1≥95 + prior3≥95) | — | 54 OOS trades — not blockable. |
| $100/trade bump | Aug 17 | Hold until balance ≥ $2,200. |
| Window-based consecutive loss | — | Group by expiry timestamp, not individual trades. |

---

# 8. Not currently pursued

**Calibration warning:** this section was previously titled "tested and rejected" and
every row said "Dead". On 2026-08-17, two of its NO-related claims failed to
replicate, and the headline "NO is -EV" verdict was overturned entirely. These are
**leads about where effort went**, so you don't redo expensive work — they are not
verdicts, and none of them can be reproduced from anything in this repo. If a
question matters, re-run it with `scripts/backtest.py` against current data.

*Volatility / dispersion filter (pre-registered, 2026-08-18):* **refuted.** Bucketing
entries by prior-candle price range (`max-min` of ask,p1,p2,p3) gives a *non-monotonic*
WR — MID is the worst bucket in both windows — and the cluster-bootstrapped HIGH-LOW
delta includes zero in-sample and out-of-sample. Decisive point: HIGH-dispersion entries
are **66% of volume and 77% of holdout profit**, so excluding them cuts holdout P&L from
+$4,242 to +$992. High dispersion is the ask *transiting* 90-93c toward 100c as certainty
resolves (Invariant 6) — the strategy working, not a danger signal. Note the prior-candle
gate (`PRIOR_MIN=75`) is already a volatility filter; this tested for residual signal after
it. Third attempt at this idea (`backtest_vol_filter.py` Aug 9, dispersion filter Aug 17
which inverted OOS). `python3 scripts/vol_bucket_test.py`

*Directional / entry variants:* longshot crash-reversal · cross-asset lag · candle
acceleration · stuck-market breakout · per-series WR kill switch · early-window entry
(600-800s) · spot-Kalshi dislocation scalp · oracle-lag final 0-150s ·
KXETH15M exclusion (would have **cost $978** — ETH is the best series; do not re-raise
on a losing streak).

*Direction-neutral structures (Aug 15-17, all negative after fees):* complete-set
accumulator · matched-pair maker · cross-crypto relative value · market-neutral pairs ·
vertical/cross-strike arb · all-taker catalog scan · maker-then-hedge · WTI ladder
maker · liquidity-reward farming · hourly range-pin · one-touch barriers.

*Other markets:* new 15M series (HYPE -$1.76/tr, NEAR, Gold, Silver) · weather
crossed-strike · cross-venue sports arb (**account is US-only** — this kills most
venue arb) · Kalshi vs sportsbook · sports-futures dominance · Fed complete set.

*Cross-listing 3-leg arb (range book vs threshold ladder):* **real and verified but
economically worthless** — 4 opportunities in 14 days worth **+$1.05 total** at 10
contracts, requiring a websocket service and non-atomic batch orders. Re-scan with
`PYTHONPATH=. python3 scripts/xlist_arb.py BTC ETH` before ever reconsidering.

Raw 2026-08-15/17 work: `~/Documents/Codex/2026-08-12/i-ran-a-full-ablation-study/work/`

---

# 9. Rules

1. Read files before editing. **Surgical edits only to the live trader — real money.**
2. Never add "while I'm here" cleanups or abstractions to the live trader.
3. Never paste secrets into chat. Never commit `.env` or key files. Repo is public.
4. **Any claim you add to this file must carry the command that reproduces it.**
5. **Evidence standard:** 500+ trades per bucket OR a pre-registered hypothesis before
   treating a slice as confirmed. Post-hoc slices are hypothesis generation only.
   1,700+ 2-way combos were tested in the ablation — nothing survives without
   preregistration.
6. **Resample by close cluster, never by trade.** Seven series settle simultaneously.
7. Prefer removing restrictions to adding filters (see Invariant 6).
8. Local main is NOT live main — always branch from `origin/main`.
9. Live state is in the GitHub Actions cache, not local `certainty_state.json`.
10. Ground-truth P&L: `daily_summary.py` or Gmail `[Kalshi]`. Never state-derived P&L.
11. Never bump `STRATEGY_VERSION` unless strategy LOGIC changes (resets cumulative stats).
12. All timestamps ET (America/New_York). Archive filenames are UTC days.
13. When results disagree with this file, **this file is probably the wrong one.**
