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

## Active Strategy — v5.13 late-certainty (Aug 15, PRs #77–#94)

**Trigger:** buy YES at ask [90, 93]¢ with 150-600s remaining, provided prior 2 same-side 1-min candles all ≥ 75¢. If ask ≤ 91¢, also requires 3rd prior candle ≥ 80¢. Hold to settlement.

**Current parameters:**
- `FLAT_BET_DOLLARS = 75` — flat per trade, no balance dependency
- `MIN_ASK_CENTS = 90`, `MAX_ASK_CENTS = 93`
- `MIN_SECS_LEFT = 150`, `MAX_SECS_LEFT = 600`
- `PRIOR_MIN_CENTS = 75`, `PRIOR_LOOKBACK = 2`
- `YES_ONLY = True` — NO side suspended (see below)
- `BLACKOUT_HOURS = {13}` — ET hour 13 (1pm ET) excluded
- `MAX_CONCURRENT_POSITIONS = 2` — correlated basket cap (see below)
- `MIN_BOOK_DEPTH = 60` — skip if fewer than 60 YES contracts at ≤93¢ (thin-book guard)
- `STOP_BALANCE = 300`
- `CONSEC_LOSS_LIMIT = 5` → 60-min cooldown
- `EDGE_DEGRADE_THRESHOLD = 0.84`, `EDGE_DEGRADE_WINDOW = 50`, `EDGE_DEGRADE_COOLDOWN = 7200`
- `HWM_DRAWDOWN_PCT = 0.10` — halt if equity drops >10% below peak (uses equity = cash + open position cost)
- `SHADOW_SERIES = ["KXHYPE15M"]` — scanned but not traded; logs `[SHADOW:]` lines

**Series (`SERIES_LIST`):** `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXDOGE15M`, `KXBNB15M`, `KXXRP15M`, `KXWTI15M`

**Order flow:**
1. `fetch_live_position_tickers()` — prevents double-orders on cache miss
2. Preflight refetch (`_fresh_ask_cents()`) — fresh ask ~200ms before order
3. Book depth check (`_book_depth_at_max_ask()`) — skip if < MIN_BOOK_DEPTH contracts available
4. Place GTC limit at `min(93, fresh_ask + 2)`
5. `sleep(3)` → cancel GTC → `sleep(0.5)` → query fills by `order_id`
6. Store position with `fee_cost` and `order_id` from fill records

**Validated backtest performance (YES-only, current config, 60-day dataset):**
- Period: 2026-06-13 → 2026-08-12
- 6,796 trades, 94.2% WR, +$9,960 total, **+$165/day**, +$1.47/trade at $75/bet
- Live since Aug 1: 1,191 trades, 95.1% WR, +$346 net (mixed bet sizes $45→$75)

## Key decisions and why

**YES_ONLY = True**
- Backtest (5,683 trades): YES +$0.87/trade vs NO +$0.23/trade. NO at 92c is -$0.34/trade, NO at 93c is +$0.01/trade. 56% of NO trades hit at 92-93c → effective NO EV is -$0.16/trade.
- Live (129 settlements): YES 46W/1L (+$116), NO 74W/8L (-$94).
- NO's backtest +$0.23 overall had a 95% CI of -$0.23 to +$0.70 — never reliably positive even at 2,896 trades.
- Keep suspended. Shadow-testing NO 90-91c (historically +$0.73/trade in old backtest, but post-hoc slice — needs 90-day clean validation).

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

**HWM drawdown halt (10%)**
- Halts if equity (cash + open position cost) drops >10% below peak equity.
- Uses equity not raw cash — opening positions doesn't false-trigger.

**Rolling 24h P&L daily loss limit**
- Daily loss limit (4× bet = $300) uses trailing 24h window via `settled_ts`.
- Prevents double-limit exposure around midnight ET.

**Filters removed Aug 12**
- H4 filter (spot momentum): net -$431 P&L over 60-day ablation. Removed.
- Near-strike filter: net -$28 P&L over 60-day ablation. Removed.
- UTC 08/22 blackout: wide CI, classic multiple-comparison problem. Reverted.
- UTC 11 (ET) blackout: ablation shows +$0.54/removed trade. Removed.

## Shadow testing

`[SHADOW:NO-90-91]` lines logged when YES_ONLY blocks a NO trade at 90-91c.
`[SHADOW:EXCL-KXHYPE15M]` lines logged when HYPE has a qualifying YES market.

Re-entry criteria (preregistered):
- Minimum: 90 calendar days + 250 unique settlement clusters + 500 executable shadow trades
- Metric: net P&L after one-tick adverse execution (not win rate)
- Inference: cluster-bootstrap CI (settlements at same close-time are one risk event, not N independent trades)
- Confidence: Holm-adjusted one-sided 98.75% lower bound > $0 per component

## Dashboard

Live at **https://polymarket-monitor2.onrender.com** (Render free tier, cold-start ~30s if idle).
- Balance, cumulative P&L chart (starts at 0 for selected range), time ranges all floor at Aug 1
- Auto-refresh every 30s. Data pulled live from Kalshi settlements + positions API.
- Render env vars: `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY` (raw PEM content, not base64)
- `kalshi_dashboard.py` is the entrypoint. `render.yaml` + `requirements.txt` control the deploy.

## Key files

| File | Purpose |
|------|---------|
| `late_certainty_trader.py` | THE live trader — surgical edits only (real money) |
| `.github/workflows/late_certainty.yml` | Workflow — cron backup + self-dispatch chain |
| `.github/workflows/daily_summary.yml` | Nightly P&L email; `--hours N` and `--trades N` flags |
| `daily_summary.py` | Ground-truth P&L from Kalshi settlements API (not state) |
| `kalshi_auth.py` | RSA-PSS-SHA256 auth wrapper + `cancel_order()` |
| `kalshi_dashboard.py` | Render-hosted Robinhood-style dashboard |
| `render.yaml` | Render deploy config |
| `certainty_state.json` | Local blank; live state is in GitHub Actions cache |
| `backtest_ablation_raw.csv` | 72,090-row ablation dataset (60-day, 7 series, both sides) |
| `backtest_ablation.py` | Ablation backtest runner |

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
5. `git push origin <branch>`
6. `TOKEN=$(git remote get-url origin | sed 's|https://\([^@]*\)@.*|\1|')`
7. `GH_TOKEN=$TOKEN gh pr create ...` then `GH_TOKEN=$TOKEN gh api repos/chrisgarceau6-dev/polymarket-monitor2/pulls/<n>/merge -X PUT -f merge_method=squash`

**Local `.claude-flow/` files** conflict on every branch checkout — always resolve with `git checkout --theirs .claude-flow/` and `git add .claude-flow/`.

## Kalshi API

- Candles: `/series/{series_ticker}/markets/{ticker}/candlesticks`
- Orderbook: `/markets/{ticker}/orderbook` → `orderbook_fp.no_dollars` (NO bid side = YES ask side via complement)
- Fills: `/portfolio/fills?ticker=<t>&order_id=<id>&limit=1000` — filter by `order_id`; `fee_cost` field has exact fee
- Positions: `/portfolio/positions?settlement_status=unsettled&limit=200`
- Settlements: `/portfolio/settlements?limit=200` — used by `daily_summary.py` for ground-truth P&L
- Orders: `/portfolio/orders?status=resting&limit=10`
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
| KXZEC15M, KXNEAR15M | Negative OOS / structural -EV. Dead. |
| New series (KXADA15M, KXBCH15M, KXTON15M) | Insufficient data. Re-check 4+ weeks. |
| H4 spot momentum filter | Net -$431 over 60-day ablation. Dead. |
| Near-strike filter | Net -$28 over 60-day ablation. Dead. |

## What's parked (not rejected, revisit with data)

| Item | When / Criteria |
|------|----------------|
| NO 90-91c re-entry | 90 days shadow data + cluster-bootstrap CI > $0 |
| KXHYPE15M re-entry | Same shadow criteria as NO |
| ET 08, 22 blackout | 500+ trades each before re-testing |
| $100/trade bump | 200 live settlements ≥93% WR (balance already >$1,200 ✓) |
| Hourly Kalshi crypto markets | Potential parallel strategy to fill dead zones |
| Thursday blackout | 349 trades at -$1.20/trade — suggestive but needs 500+ |
| BNB exclusion | 92.2% WR, +$0.06/trade YES — borderline, watch another month |
| z_cushion filter (Q1 <0.9) | 929 trades at -$0.12/trade — real signal but 95% CI not confirmed negative yet |

## Rules

1. Read files before editing. Surgical edits only to live trader — real money.
2. Never add "while I'm here" cleanups or abstractions to live trader.
3. Never paste secrets into chat. Never commit `.env` or key files.
4. **Evidence standard:** require 500+ trades per bucket OR pre-registered hypothesis before treating a backtest slice as "confirmed." Post-hoc slices across 24 hours, days of week, etc. are hypothesis generation only.
5. Local main is NOT live main — always base branches on `origin/main`.
6. Live state is in GitHub Actions cache, not local `certainty_state.json`.
7. Ground-truth P&L: run `daily_summary.py` or check Gmail `[Kalshi]` emails. Never trust state-derived P&L.
8. Run `python3 -m py_compile late_certainty_trader.py` before every push.
9. Never bump STRATEGY_VERSION unless strategy LOGIC changes (resets cumulative stats).
10. Seven series close simultaneously — they are correlated, not independent. Never treat same-expiry positions as independent risk events.
11. All timestamps are ET (America/New_York). BLACKOUT_HOURS is in ET hours.
