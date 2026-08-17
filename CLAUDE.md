# Kalshi Trading — Auto-Context

Claude Code reads this file automatically when opened in `/Users/chrisgarceau/pm/`.
You have full context. Just do what's asked. No need for the user to explain the project.

## Who / What

- Chris Garceau — UMass freshman. Real money on Kalshi. Expects concise responses, no encouragement without evidence, no emojis.
- Repo: `/Users/chrisgarceau/pm/` → GitHub `chrisgarceau6-dev/polymarket-monitor2`
- Live entrypoint: `late_certainty_trader.py` on `origin/main`, GitHub Actions workflow `.github/workflows/late_certainty.yml`
- **Primary trigger:** self-dispatch — each run sleeps ~30s then dispatches itself via `GH_DISPATCH_TOKEN` secret
- **Backup cron:** `*/5 * * * *` and `2-59/5 * * * *` (staggered) — fires if self-dispatch chain breaks
- Repo is PUBLIC — unlimited GitHub Actions minutes

## Active Strategy — v5.16 late-certainty (Aug 17)

**Trigger:** buy **either side** at ask [90, 93]¢ with 150-600s remaining, provided prior 2 same-side 1-min candles all ≥ 75¢. If ask ≤ 91¢, also requires 3rd prior candle ≥ 80¢. Hold to settlement.

**The single most important fact about this strategy:** you risk $75 to win ~$6.50,
so break-even is ~92% WR and you run ~94%. The edge is the ~2pp gap, nothing more.
One cent of entry price is worth ~1pp of break-even WR — i.e. **one tick ≈ half your
edge**. Measured execution gap is currently **+0.105¢** (see below). Protect that
number above all else; almost every parameter in this file flips sign if it degrades.

**Current parameters:**
- `FLAT_BET_DOLLARS = 75` — flat per trade, no balance dependency
- `MIN_ASK_CENTS = 90`, `MAX_ASK_CENTS = 93`
- `MIN_SECS_LEFT = 150`, `MAX_SECS_LEFT = 600`
- `PRIOR_MIN_CENTS = 75`, `PRIOR_LOOKBACK = 2`
- `YES_ONLY = False` — **both sides trade as of v5.16** (see below)
- `BLACKOUT_HOURS = set()` — no hours blocked; ET13 removed (p=0.43, noise); ET08 shadow-logged only
- `MAX_CONCURRENT_POSITIONS = 2` — correlated basket cap (see below)
- `MIN_BOOK_DEPTH = 60` — skip if fewer than 60 YES contracts at ≤93¢ (thin-book guard)
- `STOP_BALANCE = 650`
- `CONSEC_LOSS_LIMIT = 9` → 60-min cooldown (was 5; fires too often on correlated same-expiry closes)
- `EDGE_DEGRADE_THRESHOLD = 0.84`, `EDGE_DEGRADE_WINDOW = 50`, `EDGE_DEGRADE_COOLDOWN = 7200`
- `SHADOW_SERIES = ["KXHYPE15M", "KXBTCD", "KXETHD", "KXWTIH"]` — scanned but not traded

**Series (`SERIES_LIST`):** `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXDOGE15M`, `KXBNB15M`, `KXXRP15M`, `KXWTI15M`

**Order flow:**
1. `fetch_live_position_tickers()` — prevents double-orders on cache miss
2. Preflight refetch (`_fresh_ask_cents()`) — fresh ask ~200ms before order
3. Book depth check (`_book_depth_at_max_ask()`) — skip if < MIN_BOOK_DEPTH contracts available
4. Place GTC limit at `min(93, fresh_ask + 2)`
5. `sleep(3)` → cancel GTC → `sleep(0.5)` → query fills by `order_id`
6. Store position with `fee_cost` and `order_id` from fill records

**Shadow logging (in try_trade, after prior/ask gates pass):**
- `[SHADOW:C1-SOL-LOW-P2]` — SOL + prior2 75-79¢ → quarantined, no trade
- `[SHADOW:C5-HIGH-P1P3]` — prior1≥95¢ + prior3≥95¢ → shadow only, trade still fires
- `[SHADOW:ET08]` — close hour ET 8am → shadow only, trade still fires
- `[SHADOW:ET13]` — close hour ET 1pm → shadow only, trade still fires
- `[SHADOW:NO-90-91]` — **RETIRED v5.16.** NO trades live; collection disabled.
- `[SHADOW:EXCL-{series}]` — SHADOW_SERIES qualifying YES market

**Validated backtest performance (YES-only, current config, 60-day dataset):**
- Period: 2026-06-13 → 2026-08-12
- 6,796 trades, 94.2% WR, +$9,960 total, **+$165/day**, +$1.47/trade at $75/bet
- Live since Aug 1: 1,191 trades, 95.1% WR, +$346 net (mixed bet sizes $45→$75)

## Key decisions and why

**YES_ONLY = False — NO side re-enabled (v5.16, Aug 17). Supersedes the Aug 11 suspension.**

The suspension rested on live YES 46W/1L vs NO 74W/8L. That difference is **not
significant**: z=1.64, two-sided **p=0.102**, on n=47 YES trades. At the normal ~94%
rate, YES "should" have lost ~2.8 of 47 (it lost 1) and NO ~4.9 of 82 (it lost 8) —
about **five coin flips of luck**, worth roughly the entire $210 P&L gap at the bet
sizes in use. Half the available markets were cut on five trades.

Full retained history (Jun 11 – Aug 17, 67.8 days, 6,399 close clusters, 83k rows,
all 7 series — this is *everything* Kalshi still holds):

| | trades | WR | total | $/day |
|---|---|---|---|---|
| YES only | 4,129 | 93.78% | +$4,547 | +$67 |
| both sides | 8,232 | 93.62% | +$8,013 | +$118 |

At the measured +0.105¢ execution gap: **+$62/day → +$108/day**, delta +$3,134,
P(better)=0.981 (98.75% CI lower bound -$302 — strong, not airtight).

**There is no measurable YES/NO asymmetry, and no mechanism for one:**
- Cluster-bootstrapped YES-minus-NO win rate: **+0.75pp [-0.36, +1.86]** in-sample,
  **-1.59pp [-4.54, +1.35]** on holdout. Both include zero; the sign flips.
- Base rate: these markets settle YES **49.8%** of the time (n=27,908). Honest coin
  flips — no directional tilt to favour either side.
- Strike placement: spot is above the strike in 49.5% of markets. Kalshi is not
  setting strikes in a way that advantages YES.
- NO wins **93.65%**, YES **93.79%**, over 6,688 trades. Same side of the same bet.

Claims from the old regime that did **not** survive re-testing — do not resurrect:
- *"NO at 92-93c is -EV"* — did not replicate; NO was **strongest** at 92¢ in holdout.
- *"Two NOs per close cluster is -$1,211"* — the 2nd NO in a cluster matches the 1st
  (+$1.04 vs +$1.02/tr) and does **not** widen the tail: `MAX_CONCURRENT_POSITIONS=2`
  caps a cluster at $150 regardless of side. Worst cluster is -$150 either way.
- The `[SHADOW:NO-90-91]` apparatus is **retired** — the question is answered.
  Collection is disabled; settlement scoring drains existing records.

**Execution quality — the master variable (measured Aug 17)**

Reconstructed from 1,486 live fills in the 88-93¢ band (19,245 contracts):

| | |
|---|---|
| actual contract-weighted fill | 91.968¢ |
| backtest-predicted entry ask | 91.863¢ |
| **execution gap** | **+0.105¢** |

Fills are essentially at the quote, including 32 fills *below* the 90¢ floor (price
improvement). Method note: compare **distributions**, not per-fill against a candle —
a per-fill comparison gives +0.85¢ that is pure artifact, because the 1-min candle
reference is stale 47% of the time and produces textbook regression to the mean.

Sensitivity, full history: baseline +$4,510 at quote → +$1,393 at one tick. **One cent
removes 69% of all profit.** Slippage is the biggest *risk*, not the biggest
*opportunity* — it is already near zero, so guard it rather than chase it.

Also verified: the 691 non-late-certainty fills on this account are benign. 510 at
94-99¢ are the pre-v5.6.4 config (ended Aug 9); 181 sub-88¢ are crash-through fills,
and they are *profitable* (48.1% WR at ~34¢ average entry, well above the ~34%
break-even). Nothing on the account is bleeding.

**prior3≥80¢ gate at ask≤91¢**
- Cross-tab (n=2035): ask 90-91c + prior2-only = -$0.08/trade. Ask 90-91c + prior3≥80c = +$0.93/trade.
- 92-93c entries remain +EV with prior2 alone — gate only applies at ≤91c.

**MAX_CONCURRENT_POSITIONS = 2**
- All 7 series are highly correlated contracts closing simultaneously.
- Capping at 2 limits basket exposure to $150 (~10% of account).

**MIN_BOOK_DEPTH = 60**
- KXBNB and KXSOL consistently showed 1-15 contract fills instead of ~80 on thin books.
- Depth check reads NO bid side of orderbook; fails open on API error (never blocks on connectivity issues).

**EDGE_DEGRADE_THRESHOLD = 0.84 (catastrophic breaker, not regime detector)**
- Break-even WR at current entry prices is ~92%. 84% threshold tolerates ~24% account drawdown before firing.
- Treat as last-resort circuit breaker. Do NOT lower further; do NOT raise back to 88% (caused 9h false halt Aug 11).

**Position sizing — $75 is the ceiling until balance ≥ $2,200 (decided Aug 17)**

Bet size is a risk decision, not a win-rate decision. Gate it on balance, never on WR alone.

Rule: **raise bet size only when the new size is ≤ 4.6% of balance** — the ratio $75
represents at $1,631. So $100 needs ~$2,200; $125 needs ~$2,700.

Evidence (60-day live-rule sequence from a $1,631.59 start, `scripts/sizing.py`):

| | $75 | $100 |
|---|---|---|
| Net P&L 60d | +$1,035 | +$1,407 |
| Max drawdown | -$1,162 (50.2% of peak) | -$1,548 (64.7% of peak) |
| Worst single day | -$517 | -$690 |
| Days worse than -$300 | 2 | 7 |
| Per-trade % of equity | 4.6% | 6.1% |
| 2 concurrent | 9.2% | 12.3% |

Block bootstrap, 1,000 resampled 60-day runs, contiguous 3-day blocks:

| Sizing | Median worst DD | 5th pct | **P(hit $650 stop)** |
|---|---|---|---|
| $75 | -$947 | -$1,965 | **13.3%** |
| $100 | -$1,249 | -$2,653 | **24.6%** |

Why $100 was rejected: it buys **+$372** of 60-day profit for **-$386** of extra
drawdown (worse than 1:1) and roughly doubles risk of hitting the stop floor,
1-in-8 to 1-in-4, for +$6.19/day.

Note the old gate ("200 live settlements ≥93% WR") was technically MET at exactly
93.0% — it tested the wrong quantity. Break-even is ~92%, so it authorized more
leverage at a 1pp margin, and the recent 200-settlement WR (93.0%) was *below* the
Aug 1+ cohort (95.1%), i.e. the trend was softening, not confirming.

Also note $75 itself already implies a 13.3% chance of touching $650 in any 60-day
stretch. Current sizing is the aggressive end of reasonable, not the middle. Let the
balance grow into a bigger bet rather than sizing up into a good streak.

**HWM drawdown halt — REMOVED (Aug 16, v5.15)**
- Added Aug 15 with zero backtest evidence. Self-locking at $75 sizing: 2 correlated losses ≈ 9.6% of equity, making the 10% threshold unreachable in practice.
- Halted the bot for ~9h on Aug 15 (equity $1,556 vs threshold $1,565). Removed permanently.

**CONSEC_LOSS_LIMIT = 9 (was 5)**
- 7 series close simultaneously. With 5-limit, consecutive loss counter fired ~14×/60 days on pure correlation noise (every 4 days). Raised to 9 to catch genuine degradation runs only.

**ET13 blackout — REMOVED (Aug 16, v5.15)**
- WR in ET13 not meaningfully below break-even (p=0.43). Classic multiple-comparison artifact from scanning 24 hours post-hoc with no pre-registered hypothesis. Ablation showed +$0.54/trade recovered by removing. Now shadow-logged only.

**ET08 — shadow-logged only (not blocked)**
- -$2.39/trade, 89.4% WR in ablation — worse than ET13 numerically but not confirmed across 3 independent time periods. Needs 500+ trades before blocking. Shadow-logging accumulates data.

**Rolling 24h P&L daily loss limit**
- Daily loss limit (4× bet = $300) uses trailing 24h window via `settled_ts`.
- Prevents double-limit exposure around midnight ET.

**C1 provisional quarantine: KXSOL15M + prior2 75-79¢**
- **Aug 17 re-measure: worth ~$260 total over 67.8 days** (+$8,566 with vs +$8,306
  without). It is noise, not the load-bearing filter the numbers below imply. It was
  also quarantined on 60 IS + 80 OOS trades, which violates Rule 4's 500-trade bar.
  Left in place because it is harmless and reversal is not worth a live change — but
  do not cite it as precedent for a new quarantine.
- IS (60 trades): -$3.42/trade. OOS Jul13-Aug12 (80 trades): -$7.25/trade. All 3 sequential 20-day periods negative.
- Quarantined in v5.14. Shadow-logged as `[SHADOW:C1-SOL-LOW-P2]`. SOL at prior2 ≥ 80¢ still trades normally.
- Reassess after 60 calendar days + 100 prospective signals (~Oct 15, 2026).

**C5 shadow-only: prior1≥95¢ + prior3≥95¢**
- 54 OOS trades — insufficient for blocking. Logs `[SHADOW:C5-HIGH-P1P3]` but does not block.
- Note: neither C1 nor C5 survive Bonferroni correction across 1,700 2-way combos tested.

**Filters removed Aug 12**
- H4 filter (spot momentum): net -$431 P&L over 60-day ablation. Removed.
- Near-strike filter: net -$28 P&L over 60-day ablation. Removed.
- UTC 08/22 blackout: wide CI, classic multiple-comparison problem. Reverted.
- UTC 11 (ET) blackout: ablation shows +$0.54/removed trade. Removed.

## Shadow testing

**KNOWN DEFECT (Aug 17):** `[SHADOW:NO-90-91]` fires in `try_trade` BEFORE the
fresh-ask refetch and prior-candle checks, so it logs candidates that a real order
would often reject. These lines are **not executable signals** and must not be
counted toward the 90-day NO re-entry gate. Working replacement (records only
signals passing the same just-in-time gates a live order faces, with settlement
scoring) exists uncommitted in worktree `kalshi-no-shadow` — needs rebasing onto
`origin/main` before it can be shipped.

`[SHADOW:NO-90-91]` lines logged when YES_ONLY blocks a NO trade at 90-91c.
`[SHADOW:EXCL-KXHYPE15M]` lines logged when HYPE has a qualifying YES market.
`[SHADOW:ET08]` and `[SHADOW:ET13]` logged in try_trade after prior/ask gates pass.

Re-entry criteria (preregistered):
- Minimum: 90 calendar days + 250 unique settlement clusters + 500 executable shadow trades
- Metric: net P&L after one-tick adverse execution (not win rate)
- Inference: cluster-bootstrap CI (settlements at same close-time are one risk event, not N independent trades)
- Confidence: Holm-adjusted one-sided 98.75% lower bound > $0 per component

## Dashboard

Live at **https://polymarket-monitor2.onrender.com** (Render free tier, cold-start ~30s if idle).
- Balance, cumulative P&L chart (starts at 0 for selected range), time ranges all floor at Aug 1
- **Toggle**: P&L mode (cumulative from range start, green/red fill) vs Balance mode (absolute balance including deposits, blue line)
- Balance mode reconstructs from `/portfolio/deposits` + settlements, anchored to live balance via drift correction
- Auto-refresh every 30s. Data pulled live from Kalshi settlements + positions API.
- Render env vars: `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY` (raw PEM content, not base64)
- `kalshi_dashboard.py` is the entrypoint. `render.yaml` + `requirements.txt` control the deploy.

## Key files

| File | Purpose |
|------|---------|
| `late_certainty_trader.py` | THE live trader — surgical edits only (real money) |
| `scripts/archive_candles.py` | Nightly candle archival — see "Data archival" below |
| `data/candles/YYYY-MM-DD.csv.gz` | Permanent candle archive (~13 KB/day) |
| `.github/workflows/late_certainty.yml` | Workflow — cron backup + self-dispatch chain |
| `.github/workflows/daily_summary.yml` | Nightly P&L email; `--hours N` and `--trades N` flags |
| `daily_summary.py` | Ground-truth P&L from Kalshi settlements API (not state) |
| `kalshi_auth.py` | RSA-PSS-SHA256 auth wrapper + `cancel_order()` |
| `kalshi_dashboard.py` | Render-hosted Robinhood-style dashboard |
| `render.yaml` | Render deploy config |
| `certainty_state.json` | Local blank; live state is in GitHub Actions cache |
| `backtest_ablation_raw.csv` | 72,090-row ablation dataset (60-day, 7 series, both sides) |
| `backtest_ablation.py` | Ablation backtest runner |
| `losses_report.pdf` | 395 losses (60-day, v5.13 params), landscape, full trade detail |

## Auth

- Kalshi API key: stored in GitHub Secret `KALSHI_API_KEY_ID` (do not paste here — public repo)
- Correct private key: `/Users/chrisgarceau/.kalshi/private_key.pem` (NOT `pm/kalshi_key.pem`)
- Auth signature: must include `/trade-api/v2` prefix — fixed in `kalshi_auth.py` line 38
- GitHub remote URL contains PAT (see `git remote get-url origin`) — flagged for rotation

## Push flow

**Never push directly to main.** Workflow:
1. `git fetch origin && git checkout -b <branch> origin/main`
2. `git checkout --theirs .claude-flow/ && git add .claude-flow/`
3. Make changes
4. `python3 -m py_compile late_certainty_trader.py` — must pass
5. `python3 -m pytest test_order_safety.py` — all 20 tests must pass
6. `git push origin <branch>`
7. `TOKEN=$(git remote get-url origin | sed 's|https://\([^@]*\)@.*|\1|')`
8. `GH_TOKEN=$TOKEN gh pr create ...` then `GH_TOKEN=$TOKEN gh api repos/chrisgarceau6-dev/polymarket-monitor2/pulls/<n>/merge -X PUT -f merge_method=squash`

**Local `.claude-flow/` files** conflict on every branch checkout — always resolve with `git checkout --theirs .claude-flow/` and `git add .claude-flow/`.

## Kalshi API

- Candles: `/series/{series_ticker}/markets/{ticker}/candlesticks`
- Orderbook: `/markets/{ticker}/orderbook` → `orderbook_fp.no_dollars` (NO bid side = YES ask side via complement)
- Fills: `/portfolio/fills?ticker=<t>&order_id=<id>&limit=1000` — filter by `order_id`; `fee_cost` field has exact fee
- Positions: `/portfolio/positions?settlement_status=unsettled&limit=200`
- Settlements: `/portfolio/settlements?limit=200` — used by `daily_summary.py` for ground-truth P&L
- Orders: `/portfolio/orders?status=resting&limit=10`
- Deposits: `/portfolio/deposits` — returns deposit history with `amount_cents`, `fee_cents`, `finalized_ts`, `status`; filter `status=applied`
- Spot prices: Coinbase `https://api.exchange.coinbase.com/products/{pair}/candles?granularity=60` (Binance geo-blocked)
- `gh` CLI requires `GH_TOKEN` env var (extract from remote URL)
- `gh pr merge` sometimes fails — use `gh api .../pulls/N/merge -X PUT -f merge_method=squash`

## Strategy research — tested and rejected

| Strategy | Result |
|----------|--------|
| Longshot crash-reversal (buy 5-25c after prior 60c+ candles) | OOS -$1,411. Dead. |
| Cross-asset lag (buy lagging asset when leader at 90c+) | 87.3% WR WITH companion vs 90.5% WITHOUT. Dead. |
| Candle acceleration filter (require rising prior candles) | Flat/decel outperforms. Dead. |
| Stuck-market breakout (buy 65-85c after 4+ candles at 50c) | Every bucket -EV, -$8,553. Dead. |
| Per-series WR kill switch | All 36 param combos lose vs baseline. Dead. |
| **KXETH15M exclusion** | **Dead — would have COST $978 over 60 days.** ETH is the single best series in backtest: 619 trades, 95.3% WR, +$1.58/trade, and the ONLY series whose cluster-bootstrap CI95 excludes zero ([+0.15, +2.87]). Positive in all four 15-day sub-periods. Live since Aug 1 it shows -$0.35/trade over 164 trades, but that is ~0.2 SE from zero — pure noise. Do not re-raise on a short losing streak. |
| KXZEC15M, KXNEAR15M | Negative OOS / structural -EV. Dead. |
| New series (KXADA15M, KXBCH15M, KXTON15M) | Insufficient data. Re-check 4+ weeks. |
| H4 spot momentum filter | Net -$431 over 60-day ablation. Dead. |
| Near-strike filter | Net -$28 over 60-day ablation. Dead. |
| HWM drawdown halt (10%) | No backtest evidence; self-locking at $75 sizing. Removed Aug 16. |
| ET13 blackout | p=0.43, multiple-comparison noise. Removed Aug 16; shadow-logged. |

## Parallel-strategy search — exhausted Aug 15-17 (do not re-explore)

27 candidates tested and killed during the Aug 15–17 research session. Numbers are
net of exact Kalshi taker fees and one-tick adverse execution unless noted.
Raw work: `~/Documents/Codex/2026-08-12/i-ran-a-full-ablation-study/work/`
(19 `*_PREREG.md` rule locks + `*.summary.json` results).

**Direction-neutral structures — all dead**

| Candidate | Result |
|-----------|--------|
| Complete-set accumulator (early symmetric YES+NO, unwind before 600s) | 6,008 episodes, -$3,107. 72% filled one side only. Dead. |
| Matched-pair maker (two-sided quoting) | 151,788 quote cycles. Lost under BOTH optimistic-touch and conservative trade-through. 95% CI entirely below zero. Dead. |
| Cross-crypto relative value (buy NO on bullish coin, YES on bearish) | 385 validation trades, -$7.06/trade. Negative in every split. Dead. |
| Market-neutral crypto pairs (late window) | -$2.46 per $75 pair, 147 entries. Dead. |
| Early relative-dispersion pairs (minute 1) | -$0.82 per 10-contract pair, 115 windows. Dead. |
| Vertical/cross-strike arb (same ladder) | 1,736 event snapshots over 14 days, ZERO profitable after fees. Dead. |
| All-taker catalog scan (5,000+ open events) | 11,792 portfolios, ZERO fee-positive. Dead. |
| Maker-then-hedge (crypto/oil) | 245 settled events, ZERO conservative fills. Adjacent synthetic bids already at no-arb boundary. Dead. |
| Daily WTI ladder maker | 60 days, 5,502 leg configs, 2 safe quote moments, neither filled. Dead. |
| Liquidity-reward farming (Kalshi incentive program) | Rewards $0.17-0.40/market vs raw fill losses $1.30-1.92/market. Dead. |
| Hourly range-pin / modal band | 11 qualified of 500 events; -$0.27/trade on 42-signal subset. Also UNTESTABLE: candle endpoint omits unchanged bands, so "modal band" cannot be identified historically. Dead. |
| One-touch barrier contracts | Only 2 conservative crossings before close; both had YES ask already at $1.00. No quote under the 97c ceiling. Dead. |

**Directional / other-market candidates — all dead**

| Candidate | Result |
|-----------|--------|
| Early-window entry 600-700s | +$0.11/trade, only +$59 over 60 days. First 30d -$1.85/tr, last 30d +$2.51. Dead. |
| Early-window entry 700-800s | -$1.56/trade, 44 trades. Dead. |
| Early spot-Kalshi dislocation scalp | 5pt edge gate: 16 OOS trades (too sparse). 3pt gate: 114 trades, lost money, -$95 to combined portfolio. Dead. |
| Oracle-lag (final 0-150s, two-sided) | +$5,606 headline on 838 OOS signals BUT 2nd half -$1,925, 95% CI crossed zero, forward slice -$361. High-variance illusion. Dead. |
| Symmetric NO, full 90-93c range | +$0.04 per $45 trade; negative in first two chronological thirds. Dead. |
| New 15M series: HYPE / NEAR / Gold / Silver | HYPE -$1.76/tr, NEAR -$6.39, Gold -$4.81, Silver -$1.92. Spare-slot-only still -$2,735. Dead. |
| Weather crossed-strike (4pm fixed + event-driven) | ZERO executable trades across 354 city-days. By trigger time contracts are >95c or unquoted. Also: existing workspace weather model lost $3,718 over 748 trades. Dead. |
| Cross-venue sports arb (Kalshi vs Polymarket) | Global: +$9.50/7d looked real. BUT account is **US-only** — Polymarket US has ~11 open moneylines and every overlap is negative after fees. Dead for this account. |
| Kalshi vs US sportsbook (MLB/WNBA) | 48 exact matches, 30 priced orientations, ZERO candidates. Best NY maker hedge -$0.11 after reserve. Dead. |
| Sports-futures dominance (conference implies league) | 80 exact relationships across NBA/NFL/NHL/MLS, ZERO positive locked portfolios. Best -$0.23 per 10-contract pair. Dead. |
| Fed-decision complete set | Not exhaustive at contract-terms level — sub-25bp moves unbucketed, "at maximum one YES" not "exactly one". Not an arb. Dead. |

**Still alive (only two)**

| Candidate | Status |
|-----------|--------|
| NO 90-91c, max ONE NO per close cluster | See "NO 90-91c re-entry" in parked table. Directionally positive on 3 independent samples but small and never clears the gate. |
| Cross-listing 3-leg arb (range book vs threshold ladder) | **DEAD on economics** — real but worth ~$1.05 per 14 days at 10 contracts. See below. |

### Cross-listing arbitrage — real, verified, and economically worthless

Kalshi lists the same hourly BTC/ETH settlement twice: as mutually-exclusive price
bands (`between` markets in `KXBTC`/`KXETH`) and as an above/below threshold ladder
(`KXBTCD`/`KXETHD`, plus `greater` markets inside the range series itself).
NOTE: `KXBTCE`/`KXETHE` are 2024 election one-offs, NOT the range books.

Identity, for a band [A, B):  `Band(A,B) = Above(A) - Above(B)`, giving two
locked-payoff portfolios:
- Band cheap -> BUY Band YES + BUY Above(B) YES + BUY Above(A) NO = pays exactly $1
- Band rich  -> BUY Band NO  + BUY Above(A) YES + BUY Above(B) NO = pays exactly $2

Rule equivalence verified exactly on sampled BTC/ETH events: identical CF Benchmarks
index, identical 60-second averaging window, identical close time, identical
expiration value, band boundaries mapping onto the two threshold strikes.

The mispricing is real — a 14-day trade-print audit found 4 candidate minutes where
all three required taker-side prints existed, each >=10 contracts, within 250ms,
still profitable after fees plus a 3c/unit buffer. Not a stale-candle artifact.

**But the economics kill it:** those 4 opportunities were worth **+$1.05 total at
10-contract size over 14 days**. Even at 100 contracts that is ~$10 per two weeks,
against a required persistent websocket service, batched FOK orders (Kalshi batches
are **NOT atomic** — one leg can fill while another fails), and a residual-leg
neutralizer. Not worth building.

Live re-scan Aug 17 confirms nothing has changed: 562 band/threshold triples matched
across BTC+ETH open events, 40 with genuine two-sided quotes, **zero** positive.
Best edge -$0.006/contract BEFORE any execution reserve; median -$0.043.

Scanner (read-only, reusable): `scripts/xlist_arb.py` — run with
`PYTHONPATH=. python3 scripts/xlist_arb.py BTC ETH`. Places nothing. Re-run before
ever reconsidering this; do not rebuild it from scratch.

## What's parked (not rejected, revisit with data)

| Item | When / Criteria |
|------|----------------|
| ~~NO 90-91c re-entry~~ | **RESOLVED Aug 17 — shipped in v5.16 as a full symmetric NO side, not a 90-91¢ slice.** See "YES_ONLY = False" above. The 90-91¢ restriction was itself a post-hoc slice that did not replicate. |
| KXHYPE15M re-entry | Same shadow criteria as NO. (Aug 17: HYPE as a full series tested at -$1.76/trade — see kill-list.) |
| ET 08 blackout | 500+ shadow trades + confirmed negative across 3 time periods |
| ET 13 re-blocking | 500+ shadow trades; currently p=0.43 (noise) |
| $100/trade bump | **Gate replaced Aug 17 — now balance-based: hold until balance ≥ $2,200.** The old "200 settlements ≥93% WR" gate was met (exactly 93.0%) but tested the wrong thing. See "Position sizing" below. |
| Hourly Kalshi crypto markets | Potential parallel strategy to fill dead zones |
| Thursday blackout | 349 trades at -$1.20/trade — suggestive but needs 500+ |
| BNB exclusion | 92.2% WR, +$0.06/trade YES — borderline, watch another month |
| z_cushion filter (Q1 <0.9) | 929 trades at -$0.12/trade — real signal but 95% CI not confirmed negative yet |
| C1 quarantine reassessment | ~Oct 15, 2026 (60 days + 100 prospective signals) |
| C5 blocking | Needs 500+ shadow trades; currently 54 OOS — not blockable |
| Window-based consecutive loss | Group by common expiry timestamp; count losing windows not individual trades |

## Data archival — read this before proposing any new research

**Kalshi retains settled markets for only ~67 days.** This single fact has blocked
every strategy question this project has asked. When a hypothesis is formed, the only
data that exists is the data it was formed on, and the "out-of-sample" window is
whatever few days fall outside the discovery set. The NO-side question sat unresolved
for months purely because its re-entry gate demanded 90 days of clean holdout that the
API can never supply — on Aug 17 the entire available holdout was **7.6 days**.

`.github/workflows/archive_candles.yml` now runs nightly and commits
`data/candles/YYYY-MM-DD.csv.gz` (all 7 series, both sides, ask 88-96¢, 100-800s —
deliberately wider than the live gates). Schema matches `backtest_ablation_raw.csv`
minus `profit`/`spot_*`; `profit` is derivable from `ask` + `won`.

**Every archived day is untouched out-of-sample data for every hypothesis formed after
it.** Backfill with `--backfill N`. Never delete this directory. If the workflow has
been failing, fixing it outranks whatever research prompted the check — a day not
archived is validation capacity permanently destroyed.

## Rules

1. Read files before editing. Surgical edits only to live trader — real money.
2. Never add "while I'm here" cleanups or abstractions to live trader.
3. Never paste secrets into chat. Never commit `.env` or key files.
4. **Evidence standard:** require 500+ trades per bucket OR pre-registered hypothesis before treating a backtest slice as "confirmed." Post-hoc slices across 24 hours, days of week, etc. are hypothesis generation only.
5. Local main is NOT live main — always base branches on `origin/main`.
6. Live state is in GitHub Actions cache, not local `certainty_state.json`.
7. Ground-truth P&L: run `daily_summary.py` or check Gmail `[Kalshi]` emails. Never trust state-derived P&L.
8. Run `python3 -m py_compile late_certainty_trader.py` before every push.
9. Run `python3 -m pytest test_order_safety.py` — all 20 tests must pass before merging.
10. Never bump STRATEGY_VERSION unless strategy LOGIC changes (resets cumulative stats).
11. Seven series close simultaneously — they are correlated, not independent. Never treat same-expiry positions as independent risk events.
12. All timestamps are ET (America/New_York). BLACKOUT_HOURS is in ET hours.
13. Multiple-comparison problem: 1,700+ 2-way combos tested in ablation → Bonferroni correction required. No post-hoc slice survives without preregistration + 500+ trades.
