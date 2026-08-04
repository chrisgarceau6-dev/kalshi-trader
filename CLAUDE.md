# Kalshi Trading — Auto-Context

Claude Code reads this file automatically when opened in `/Users/chrisgarceau/pm/`.
You have full context. Just do what's asked. No need for the user to explain the project.

## Who / What

- Chris Garceau — UMass freshman. Real money on Kalshi. Expects concise responses, no encouragement without evidence, no emojis.
- Repo: `/Users/chrisgarceau/pm/` → GitHub `chrisgarceau6-dev/polymarket-monitor2`
- Live entrypoint: `late_certainty_trader.py` on `origin/main`, GitHub Actions cron `.github/workflows/late_certainty.yml`
- Cron: `10-14,25-29,40-44,55-59 * * * *` (fires 5 min before/after each 15-min boundary)

## Active Strategy — v5 late-certainty (deployed 2026-08-01)

**Trigger:** buy YES or NO at ask 90-99¢ with 150-900s remaining, provided prior 3 same-side 1-min candles all ≥ 80¢.

**Filters** (added 2026-08-02, OOS-validated):
- H4: skip if underlying spot moved > 5 bps adverse in last 60s (Coinbase)
- Near-strike: skip if `|spot - strike| / spot < 10 bps`
- Both filters fail OPEN (proceed with trade) on Coinbase API error

**Series** (in `SERIES_LIST`):
`KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXDOGE15M`, `KXBNB15M`, `KXXRP15M`, `KXHYPE15M`, `KXNEAR15M`

**Sizing and kill switches** (in `late_certainty_trader.py` ~line 144-158):
- `compute_bet_dollars()` → flat `5` (change here to adjust bet size)
- `STOP_BALANCE = 350` (halt if balance drops here)
- `CONSEC_LOSS_LIMIT = 5` → 60-min cooldown
- `compute_daily_loss_limit()` → `max(30, bet * 8)` = $40 at $5 bets

**Backtest:** 96% WR, ~$95/day @ $50 bets, ~265 trades/day across 7+ series.

**Live performance since deploy (Aug 1-3):** Balance ~$368 → ~$380, ~$4.67/day at $5 bets.

## Key files

| File | Purpose |
|------|---------|
| `late_certainty_trader.py` | THE live trader — surgical edits only (real money) |
| `.github/workflows/late_certainty.yml` | Cron workflow |
| `kalshi_auth.py` | RSA-PSS-SHA256 auth wrapper |
| `certainty_state.json` | Local blank; live state is in GitHub Actions cache |
| `certainty.log` | Local only, pre-deploy test runs |
| `HANDOFF.md` | Extended decision log and rejected filter archive |

**Halted (do not enable):** `crypto15m_trader.py` + `.github/workflows/crypto15m_trader.yml` — lost -$92, hard guard in code.

## Auth

- Kalshi API key: `972daba8-94c6-410d-ae83-5ff9852dd31a` (in GitHub Secret `KALSHI_API_KEY_ID`)
- Private key: `/Users/chrisgarceau/pm/kalshi_key.pem` (DO NOT OVERWRITE)
- Auth signature: must include `/trade-api/v2` prefix — already fixed in `kalshi_auth.py` line 38
- GitHub remote URL contains PAT (see git remote get-url origin) — flagged for rotation

## Push flow

**Never push directly to main.** The auto-classifier blocks it. Workflow:
1. `git checkout -b <branch> origin/main`
2. Make changes
3. `git push origin <branch>`
4. Create PR via `gh pr create`
5. User merges in browser (classifier blocks Claude from merging via API)

**Local main ≠ live main.** Local has an orphan commit (`Halt Kalshi Forward Paper Collector schedule`) that would kill live trading if pushed. Always base new branches on `origin/main`.

## Kalshi API gotchas

- Candles endpoint: `/series/{series_ticker}/markets/{ticker}/candlesticks` (NOT `/markets/{ticker}/candlesticks`)
- Binance is geo-blocked (HTTP 451) — use Coinbase: `https://api.exchange.coinbase.com/products/{pair}/candles?granularity=60` with `User-Agent` header
- Coinbase pair map is in `COINBASE_PAIR` dict in `late_certainty_trader.py` (~line 69)
- KXHYPE15M uses Hyperliquid API (`HYPERLIQUID_PAIR` dict, `hyperliquid_1min_close()`); H4/near-strike filters now active for HYPE
- `--daemon` flag runs continuous 20s polling loop (for VPS deployment)

## What's parked / rejected

| Item | Status |
|------|--------|
| Entry-timing filter `secs_left ≤ 300` | Parked — test showed 99.2% WR but train didn't confirm. Re-run in 2-3 weeks. |
| Confidence-scaled sizing | Rejected — marginal improvement, kills volume |
| Kalshi partial hedge | Rejected — hedge cost > tail savings at 97%+ WR |
| Commodities (KXWTI/KXGOLD/KXSILVER) | Parked — insufficient data, re-run in 2-4 weeks |
| KXZEC15M | Rejected — negative OOS |

## Rules

1. Read files before editing. Surgical edits only to live trader — real money.
2. Do not add "while I'm here" cleanups, abstractions, or logging to live trader.
3. Never paste secrets into chat. Never commit `.env` or key files.
4. Walk-forward OOS validation required before deploying any new filter.
5. Local main is NOT live main — always base branches on `origin/main`.
6. Live state is in GitHub Actions cache, not local `certainty_state.json`.
7. To check live performance: use Gmail MCP (`search_threads` for `[Kalshi-C]` subject).
