# Codex clean-room inventory

Generated 2026-08-24 and closed against current `main` after the independence hold. Polymarket and Momentum Core are excluded by charter. `.git/`, `venv/`, bytecode/test caches, `hunt_logs/`, and the nested Ruflo cache under `research/perp_overlay/` are generated dependency/control data rather than project source or evidence and are excluded. Audit work papers created under `docs/audit/claude/` and `docs/audit/codex/` after the starting census are also excluded from the audited subject to avoid a self-referential denominator; `docs/audit/CHARTER.md` remains an inventoried contract. `scripts/verify.py`, which moved from audit deliverable to current operational measurement, is included.

**Corrected census.** The Session 1 projection silently omitted `_tmp_*.csv`; the glob is now an explicit grouped row and its producers identify it as excluded Polymarket wallet-screen output. A tracked-file reconciliation then found further omissions. The table now also carries grouped rows for every tracked Polymarket file and for post-census audit work papers, adds the previously omitted Ruflo `current.json` and current `scripts/verify.py`, and gives the two Kalshi cross-venue outputs individual evidence rows. `COMPLETENESS.md` contains the exact exclusion manifest and a `comm` gate that starts from `git ls-files`, so a curated table can no longer conceal a tracked omission.

## Reproduction

Run from the repository root. The following commands reproduce the raw census and tracked-file cross-check; the table is the scoped projection of those results plus the Finance search.

```bash
find . -path './.git' -prune -o -path './venv' -prune -o -path './docs/audit/claude' -prune -o -type f -print | LC_ALL=C sort
git ls-files | LC_ALL=C sort
find "$HOME/Downloads/Finance" -maxdepth 1 -type f   \( -iname '*kalshi*' -o -iname '*weather*' \) -print | LC_ALL=C sort
find . -maxdepth 1 -type f -name '_tmp_*.csv' -print | wc -l
find . -maxdepth 1 -type f -name '_tmp_*.csv' -exec stat -f '%z' {} + | awk '{s+=$1} END {print s}'
git ls-files '_tmp_*.csv' | wc -l
git grep -n '_tmp_.*csv' -- strategy_dissect.py backtest_us_portfolio.py
awk -F'|' '/^\| `/ {n++} END {print n}' docs/audit/codex/INVENTORY.md
```

Per-row size and modification time are reproducible with `stat -f '%z %Sm' -t '%Y-%m-%dT%H:%M:%S%z' PATH`; the last-touch field is reproducible with `git log -1 --date=short --format='%h %ad %s' -- PATH`. `UNTRACKED` means that command returned no commit. External Finance files have no repository commit by definition.

## Reachability graph

Reachability is syntactic, starting at what the live workflow invokes and following local imports and explicit file resources:

```text
.github/workflows/late_certainty.yml
├── python -m unittest -v test_order_safety.py
│   ├── import late_certainty_trader
│   └── import kalshi_auth
├── python late_certainty_trader.py --daemon --duration 900 --interval 15
│   └── import kalshi_auth
└── restore/save certainty_state.json
```

Reproduce the local import/resource edges with:

```bash
rg -n 'python |^(from|import) |STATE_FILE|certainty_state.json'   .github/workflows/late_certainty.yml late_certainty_trader.py   kalshi_auth.py test_order_safety.py
```

## File census

| path | size (bytes) | last modified | last commit that touched it | REACHABLE FROM THE LIVE PATH | tier A-E | status |
|---|---:|---|---|:---:|:---:|---|
| `.DS_Store` | 6148 | 2026-07-25T16:34:30-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M01 |
| `.claude/RESUME.md` | 696 | 2026-08-18T22:51:17-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `.claude/settings.local.json` | 87 | 2026-07-24T00:23:53-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `.claude-flow/data/pending-insights.jsonl` | 37920 | 2026-08-24T13:56:35-04:00 | 8271ab8a 2026-08-04 Medium fixes: order cancellation, auto-scaling bets, heat cap, edge degradation (#5) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/current.json` | 218 | 2026-08-04T01:04:30-04:00 (HEAD; worktree deletion pre-exists audit) | 8271ab8a 2026-08-04 Medium fixes: order cancellation, auto-scaling bets, heat cap, edge degradation (#5) | no | E | audited-retain-tooling |
| `.claude-flow/sessions/session-1784867033854.json` | 284 | 2026-07-25T16:44:39-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785026831013.json` | 282 | 2026-07-25T22:13:52-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785032185632.json` | 283 | 2026-07-26T15:59:46-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785096124672.json` | 282 | 2026-07-26T18:25:42-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785104875189.json` | 281 | 2026-07-26T18:52:08-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785106465521.json` | 282 | 2026-07-26T20:54:59-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785113839613.json` | 281 | 2026-07-26T21:49:46-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785117096838.json` | 281 | 2026-07-26T22:44:43-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785120452538.json` | 282 | 2026-07-26T23:45:18-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785124062105.json` | 281 | 2026-07-27T00:26:18-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785176975388.json` | 282 | 2026-07-27T15:23:58-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785180365933.json` | 283 | 2026-07-28T01:39:30-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785818495392.json` | 285 | 2026-08-21T00:48:01-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785954319143.json` | 282 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785962941087.json` | 283 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1785980776449.json` | 283 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786037255302.json` | 281 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786043159492.json` | 283 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786148402866.json` | 283 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786164843331.json` | 283 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786197987381.json` | 281 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786199701153.json` | 280 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786200634199.json` | 280 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786201585756.json` | 280 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786202664910.json` | 282 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786222737465.json` | 282 | 2026-08-17T17:15:22-04:00 | 57abcb93 2026-08-08 Switch to flat  bet (targets ~$40/day at live trade rate) (#40) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786578785157.json` | 282 | 2026-08-17T17:15:22-04:00 | e1785362 2026-08-13 Add Render-hosted Robinhood-style dashboard (#61) | no | E | audited-retain-artifact |
| `.claude-flow/sessions/session-1786847951778.json` | 280 | 2026-08-17T17:15:22-04:00 | 88c8e77d 2026-08-16 dashboard: add P&L / Balance chart toggle (#97) | no | E | audited-retain-artifact |
| `.env` | 193 | 2026-08-19T17:57:56-04:00 | UNTRACKED | no | E | audited-sensitive-retain |
| `_tmp_*.csv (grouped; 3,538 files)` | 257690396 | 2026-07-24T00:30:02-04:00 through 2026-07-25T13:05:31-04:00 | UNTRACKED | no | E | excluded-polymarket-generated |
| `.github/workflows/archive_candles.yml` | 6448 | 2026-08-21T11:34:13-04:00 | 14494d44 2026-08-21 archive: serialise runs and survive a lost push race (#145) | no | B | audited-retain-producer |
| `.github/workflows/clean_day_measurement.yml` | 12226 | 2026-08-23T19:28:56-04:00 | df4858b7 2026-08-23 measurement: materialize the API key, and stop tee eating failures (#175) | no | B | audited-retain-producer |
| `.github/workflows/daily_summary.yml` | 1254 | 2026-08-17T17:15:22-04:00 | 81ce7a22 2026-08-11 Add --trades N mode to pull last N settlements from Kalshi API (#53) | no | B | audited-retain-producer |
| `.github/workflows/dashboard_keepalive.yml` | 1336 | 2026-08-23T14:28:39-04:00 | 43e8816f 2026-08-23 docs: note that the keepalive host is correct despite the name mismatch (#172) | no | D | audited-retain-research |
| `.github/workflows/late_certainty.yml` | 5037 | 2026-08-22T13:45:21-04:00 | b444654f 2026-08-22 scan: 900s jobs — duty cycle 94.1% -> 98.4% (#157) | yes | A | audited-retain-live |
| `.gitignore` | 576 | 2026-08-19T18:04:42-04:00 | 04b991c8 2026-08-19 chore: stop tracking the incentive API dumps (#136) | no | E | audited-retain-artifact |
| `CLAUDE.md` | 77681 | 2026-08-24T13:45:36-04:00 | ee5cadd4 2026-08-24 docs: audit autonomy, depth calibration, and the rounding finding | no | C | audited-retain-evidence |
| `backtest_85c_btc.csv` | 216932 | 2026-08-08T01:11:41-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_ablation.py` | 20998 | 2026-08-12T18:45:59-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_ablation_raw.csv` | 7313139 | 2026-08-12T18:33:12-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_ablation_spot.csv` | 13347754 | 2026-08-12T14:31:06-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_acceleration.csv` | 596444 | 2026-08-09T03:16:44-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_acceleration.py` | 6978 | 2026-08-08T23:55:12-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_ask_floor.csv` | 1389127 | 2026-08-07T20:10:02-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_ask_floor.py` | 10814 | 2026-08-06T00:25:03-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_blackout_hours.csv` | 69630 | 2026-08-09T20:50:42-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_blackout_hours.py` | 7824 | 2026-08-09T19:34:10-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_breakout.csv` | 724194 | 2026-08-09T03:48:22-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_breakout.py` | 7741 | 2026-08-09T00:26:56-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_btcd.py` | 11404 | 2026-08-14T13:13:05-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_buckets.csv` | 140325 | 2026-08-06T00:05:29-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_buckets_60d.csv` | 1111573 | 2026-08-06T14:13:20-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_commodities.py` | 8334 | 2026-08-17T17:15:22-04:00 | 7272d0cc 2026-08-13 v5.8: add KXWTI15M (94.5% WR full, 95.5% OOS, +$1.75/trade); exclude gold and silver (#71) | no | B | audited-move M03 |
| `backtest_commodities_filter.csv` | 30948 | 2026-08-05T23:23:04-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_commodities_nofilter.csv` | 96398 | 2026-08-05T23:27:04-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_cross_asset.csv` | 40032 | 2026-08-05T22:48:41-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_cross_asset.py` | 11936 | 2026-08-05T22:29:27-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_filter_audit.csv` | 311697 | 2026-08-11T20:17:51-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_filter_audit.py` | 9567 | 2026-08-09T19:42:46-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_hourly.py` | 9277 | 2026-08-13T23:46:06-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_longshot.csv` | 81131 | 2026-08-07T23:50:34-04:00 | UNTRACKED | no | E | audited-move M03 |
| `backtest_longshot.py` | 9775 | 2026-08-17T17:15:22-04:00 | a230c42f 2026-08-08 Deploy crash-reversal longshot strategy alongside main — $20 OOS trial (#33) | no | B | audited-move M03 |
| `backtest_longshot_oos.csv` | 21339 | 2026-08-08T00:53:41-04:00 | UNTRACKED | no | E | audited-move M03 |
| `backtest_multistrike.py` | 10585 | 2026-08-14T00:18:18-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_no_filter.csv` | 639508 | 2026-08-05T23:08:48-04:00 | UNTRACKED | no | E | audited-move M02 |
| `backtest_run.log` | 6833 | 2026-08-01T03:20:45-04:00 | UNTRACKED | no | E | audited-move M04 |
| `backtest_series_pause.py` | 4365 | 2026-08-11T00:22:02-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_trades.csv` | 85764 | 2026-08-23T20:53:47-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M11 |
| `backtest_ultralate.py` | 6664 | 2026-08-09T12:36:47-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_us_portfolio.png` | 114460 | 2026-07-25T21:05:21-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | UNKNOWN | excluded-polymarket |
| `backtest_vol_filter.py` | 8625 | 2026-08-09T14:42:02-04:00 | UNTRACKED | no | B | audited-move M02 |
| `backtest_wti_optimize.py` | 15178 | 2026-08-17T17:15:22-04:00 | 7272d0cc 2026-08-13 v5.8: add KXWTI15M (94.5% WR full, 95.5% OOS, +$1.75/trade); exclude gold and silver (#71) | no | B | audited-move M03 |
| `band_arb_live.csv` | 65033 | 2026-07-23T10:47:49-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M12 |
| `band_arb_sets.csv` | 621 | 2026-07-23T01:57:51-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M12 |
| `certainty.log` | 132740 | 2026-08-17T19:59:54-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `certainty_state.json` | 145 | 2026-08-20T00:28:41-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | yes | A | audited-retain-live |
| `chatgpt_review_prompt.txt` | 3351 | 2026-08-14T12:32:28-04:00 | UNTRACKED | no | UNKNOWN | audited-move M13 |
| `coinbase_fetch.log` | 608 | 2026-08-01T11:28:56-04:00 | UNTRACKED | no | E | audited-move M04 |
| `crypto15m.log` | 7759 | 2026-07-27T14:13:45-04:00 | UNTRACKED | no | E | audited-move M04 |
| `daily_summary.py` | 10178 | 2026-08-20T20:20:36-04:00 | 73b5afeb 2026-08-20 monitor: alert when the candle archive fails or goes stale (#139) | no | B | audited-retain-producer |
| `data/.btc_spot_cache.json` | 1327546 | 2026-08-18T20:55:57-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `data/candles/2026-06-11.csv.gz` | 11878 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-12.csv.gz` | 14118 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-13.csv.gz` | 13538 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-14.csv.gz` | 14203 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-15.csv.gz` | 15338 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-16.csv.gz` | 14423 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-17.csv.gz` | 13558 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-18.csv.gz` | 13274 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-19.csv.gz` | 16059 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-20.csv.gz` | 14406 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-21.csv.gz` | 15493 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-22.csv.gz` | 14178 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-23.csv.gz` | 14600 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-24.csv.gz` | 15128 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-25.csv.gz` | 12889 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-26.csv.gz` | 14473 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-27.csv.gz` | 14259 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-28.csv.gz` | 14358 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-29.csv.gz` | 13937 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-06-30.csv.gz` | 14352 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-01.csv.gz` | 14234 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-02.csv.gz` | 13716 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-03.csv.gz` | 14051 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-04.csv.gz` | 14952 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-05.csv.gz` | 13788 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-06.csv.gz` | 13124 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-07.csv.gz` | 14597 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-08.csv.gz` | 14681 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-09.csv.gz` | 12703 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-10.csv.gz` | 14348 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-11.csv.gz` | 12900 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-12.csv.gz` | 13562 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-13.csv.gz` | 12553 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-14.csv.gz` | 12655 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-15.csv.gz` | 13358 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-16.csv.gz` | 12661 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-17.csv.gz` | 15467 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-18.csv.gz` | 14270 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-19.csv.gz` | 14561 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-20.csv.gz` | 12884 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-21.csv.gz` | 13383 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-22.csv.gz` | 13482 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-23.csv.gz` | 11609 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-24.csv.gz` | 13852 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-25.csv.gz` | 12474 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-26.csv.gz` | 13599 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-27.csv.gz` | 13382 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-28.csv.gz` | 13622 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-29.csv.gz` | 14533 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-30.csv.gz` | 11852 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-07-31.csv.gz` | 13917 | 2026-08-17T17:15:22-04:00 | d2329425 2026-08-17 chore: canonical backtest harness, backfilled archive, CLAUDE.md restructure (#106) | no | E | audited-retain-artifact |
| `data/candles/2026-08-01.csv.gz` | 14306 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-02.csv.gz` | 14134 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-03.csv.gz` | 17302 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-04.csv.gz` | 21790 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-05.csv.gz` | 20754 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-06.csv.gz` | 18510 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-07.csv.gz` | 19565 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-08.csv.gz` | 15439 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-09.csv.gz` | 14445 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-10.csv.gz` | 19738 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-11.csv.gz` | 20489 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-12.csv.gz` | 19954 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-13.csv.gz` | 18691 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-14.csv.gz` | 20100 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-15.csv.gz` | 13874 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-16.csv.gz` | 14026 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-17.csv.gz` | 21506 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-18.csv.gz` | 21327 | 2026-08-19T11:05:32-04:00 | e422efe4 2026-08-19 data: archive candles 2026-08-19 | no | E | audited-retain-artifact |
| `data/candles/2026-08-19.csv.gz` | 21188 | 2026-08-20T00:28:41-04:00 | a886dc1d 2026-08-20 data: archive candles 2026-08-20 | no | E | audited-retain-artifact |
| `data/candles/2026-08-20.csv.gz` | 20257 | 2026-08-21T00:12:47-04:00 | d6d7566a 2026-08-21 data: archive candles 2026-08-21 | no | E | audited-retain-artifact |
| `data/candles/2026-08-21.csv.gz` | 21090 | 2026-08-22T11:51:32-04:00 | f0e8703c 2026-08-22 data: archive candles 2026-08-22 | no | E | audited-retain-artifact |
| `data/candles/2026-08-22.csv.gz` | 17341 | 2026-08-23T00:21:40-04:00 | 46cb379f 2026-08-23 data: archive candles 2026-08-23 | no | E | audited-retain-artifact |
| `data/candles/2026-08-23.csv.gz` | 17287 | 2026-08-24T00:28:51-04:00 | 46ad23b3 2026-08-24 data: archive candles 2026-08-24 | no | E | audited-retain-artifact |
| `data/gatelog/2026-08-22.csv` | 20294 | 2026-08-23T19:26:59-04:00 | 8cd00e2e 2026-08-23 data: gate-log cache through 2026-08-22 | no | E | audited-retain-artifact |
| `data/gatelog/2026-08-23.csv` | 31286 | 2026-08-24T00:28:51-04:00 | 5c229a21 2026-08-24 data: gate-log cache through 2026-08-23 | no | E | audited-retain-artifact |
| `data/gatelog/2026-08-24.csv` | 583 | 2026-08-24T00:28:51-04:00 | 5c229a21 2026-08-24 data: gate-log cache through 2026-08-23 | no | E | audited-retain-artifact |
| `data/gatelog/_runs.json` | 3510 | 2026-08-24T00:28:51-04:00 | 5c229a21 2026-08-24 data: gate-log cache through 2026-08-23 | no | E | audited-retain-artifact |
| `docs/audit/CHARTER.md` | 8271 | 2026-08-24T13:45:36-04:00 | ee5cadd4 2026-08-24 docs: audit autonomy, depth calibration, and the rounding finding | no | E | audited-retain-artifact |
| `docs/audit/{claude,codex}/** (grouped audit work papers)` | DYNAMIC | current audit | post-census audit commits | no | E | excluded-audit-work-product |
| `docs/websocket_spec.md` | 46226 | 2026-08-22T15:46:07-04:00 | 5f1b4f82 2026-08-22 docs: correct the record — the WS delta path is validated, my test was broken (#169) | no | E | audited-retain-artifact |
| `expand_candles.log` | 2013 | 2026-08-01T14:21:59-04:00 | UNTRACKED | no | E | audited-move M04 |
| `heartbeat.sh` | 913 | 2026-08-06T18:20:03-04:00 | UNTRACKED | no | D | audited-move M05 |
| `kalshi_auth.py` | 8635 | 2026-08-17T17:15:22-04:00 | 51137594 2026-08-14 fix: harden Kalshi order execution and exposure controls | yes | A | audited-retain-live |
| `kalshi_calib.csv` | 41305 | 2026-07-24T02:06:07-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M11 |
| `kalshi_calibration.csv` | 30875 | 2026-08-23T20:50:31-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M11 |
| `kalshi_dashboard.py` | 68701 | 2026-08-23T19:26:59-04:00 | 7f84f642 2026-08-23 monitor: fees belong inside break-even (#174) | no | B | audited-retain-producer |
| `kalshi_key.pem` | 1675 | 2026-07-26T16:20:22-04:00 | UNTRACKED | no | E | audited-sensitive-retain |
| `kalshi_pnl_chart.png` | 106424 | 2026-08-08T22:04:28-04:00 | UNTRACKED | no | E | audited-move M04 |
| `kalshi_promo_hedge.py` | 14828 | 2026-07-24T01:58:17-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | D | audited-move M12 |
| `kalshi_settlements.json` | 512341 | 2026-08-08T21:54:18-04:00 | UNTRACKED | no | E | audited-move M04 |
| `kalshi_snapshot.json` | 71439 | 2026-07-25T09:03:14-04:00 | UNTRACKED | no | E | audited-move M04 |
| `kalshi_trade_history.json` | 2315812 | 2026-08-08T21:55:42-04:00 | UNTRACKED | no | E | audited-move M04 |
| `kalshi_weather_edge.py` | 94805 | 2026-07-22T16:50:54-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | D | audited-move M11 |
| `late_certainty_trader.py` | 100498 | 2026-08-24T12:22:34-04:00 | 584d0a85 2026-08-24 v5.17: extend the entry band to 88-89c on the YES side only (#183) | yes | A | audited-retain-live |
| `liquidity_reward_candidates.csv` | 2743 | 2026-07-24T00:18:59-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M12 |
| `losses_report.pdf` | 57577 | 2026-08-16T12:54:47-04:00 | UNTRACKED | no | E | audited-move M04 |
| `render.yaml` | 676 | 2026-08-17T22:14:53-04:00 | 26d7c312 2026-08-17 dashboard: require auth, read blackout from trader, add trader health pill (#110) | no | E | audited-retain-artifact |
| `requirements.txt` | 28 | 2026-08-17T17:15:22-04:00 | e1785362 2026-08-13 Add Render-hosted Robinhood-style dashboard (#61) | no | E | audited-retain-artifact |
| `research/capture/audit.py` | 5236 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M06 |
| `research/capture/audit2.py` | 5154 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M06 |
| `research/capture/mechanism.py` | 5446 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M06 |
| `research/capture/momentum_link.py` | 4286 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M06 |
| `research/capture/refine.py` | 4974 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M06 |
| `research/capture/substitutions.py` | 4503 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M06 |
| `research/hourly_crypto/analyze_hourly.py` | 5901 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-move M07 |
| `research/hourly_crypto/analyze_hourly2.py` | 3369 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-move M07 |
| `research/hourly_crypto/build_hourly.py` | 6163 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-move M07 |
| `research/hourly_crypto/hourly_KXBTCD.csv.gz` | 87103 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | E | audited-move M07 |
| `research/hourly_crypto/hourly_KXETHD.csv.gz` | 52666 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | E | audited-move M07 |
| `research/kalshi_incentives/README.md` | 2361 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | E | audited-move M08 |
| `research/kalshi_incentives/analyze_corpus.py` | 3563 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | B | audited-move M08 |
| `research/kalshi_incentives/fetch_programs.py` | 3158 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | B | audited-move M08 |
| `research/kalshi_incentives/full_pull.log` | 209 | 2026-08-18T21:44:57-04:00 | UNTRACKED | no | E | audited-move M08 |
| `research/kalshi_incentives/regimes.log` | 159 | 2026-08-18T21:57:02-04:00 | UNTRACKED | no | E | audited-move M08 |
| `research/kalshi_incentives/scan_live.py` | 7300 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | B | audited-move M08 |
| `research/kalshi_incentives/scan_regimes.py` | 4163 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | B | audited-move M08 |
| `research/kalshi_incentives/volume_math.py` | 4169 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | B | audited-move M08 |
| `research/loss_cooldown/measure.py` | 6798 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M09 |
| `research/loss_cooldown/size_up.py` | 5471 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M09 |
| `research/loss_cooldown/steelman.py` | 4874 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M09 |
| `research/perp_overlay/.gitignore` | 31 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | E | audited-retain-artifact |
| `research/perp_overlay/PREREG.md` | 5076 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | C | audited-retain-evidence |
| `research/perp_overlay/closed_form.py` | 1938 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/features_cache.pkl` | 20357373 | 2026-08-21T11:24:15-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/perp_overlay/fetch_spot.py` | 2282 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/final_validation.py` | 4622 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/h_filter.py` | 3968 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/h_hedge.py` | 6799 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/h_quality.py` | 2286 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/h_signals.py` | 1063 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/parity_audit.py` | 5370 | 2026-08-20T20:12:57-04:00 | 9fdf8722 2026-08-20 fix: parity audit must exclude the forming bucket too | no | B | audited-retain-producer |
| `research/perp_overlay/perp_overlay.py` | 11616 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/research.py` | 10376 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/s1_recent.py` | 1461 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/s1_robustness.py` | 3208 | 2026-08-20T17:03:08-04:00 | d22512e5 2026-08-20 shadow: log adverse spot momentum, and record what perps can and cannot do (#137) | no | B | audited-retain-producer |
| `research/perp_overlay/spot_BNB-USD.json` | 1602037 | 2026-08-21T11:23:25-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/perp_overlay/spot_BTC-USD.json` | 2496396 | 2026-08-21T11:20:59-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/perp_overlay/spot_DOGE-USD.json` | 2170781 | 2026-08-21T11:21:54-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/perp_overlay/spot_ETH-USD.json` | 2388325 | 2026-08-21T11:20:59-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/perp_overlay/spot_SOL-USD.json` | 2185827 | 2026-08-21T11:21:00-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/perp_overlay/spot_XRP-USD.json` | 2287856 | 2026-08-21T11:21:54-04:00 | UNTRACKED | no | E | audited-retain-artifact |
| `research/projection/capacity.py` | 3301 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M10 |
| `research/projection/forward.py` | 4862 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-move M10 |
| `research/top5/critique_check.py` | 7272 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-retain-producer |
| `research/top5/probe.py` | 7147 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-retain-producer |
| `research/top5/sizing_validate.py` | 5417 | 2026-08-21T00:37:03-04:00 | a261d77e 2026-08-21 docs: add §10 running state, record the night's findings, keep this file current (#144) | no | B | audited-retain-producer |
| `tracked Polymarket files (grouped charter exclusion)` | 8580477 | 2026-07-20T17:43:36-04:00 through 2026-08-02T17:21:03-04:00 | tracked; exact paths in `COMPLETENESS.md` | no | E | excluded-polymarket-tracked |
| `rewards_universe.csv` | 71961 | 2026-07-24T01:13:27-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M12 |
| `scripts/archive_candles.py` | 11016 | 2026-08-24T00:27:56-04:00 | a7bf1b36 2026-08-22 archive: store exact cents — rounding was manufacturing false candidates (#163) | no | B | audited-retain-producer |
| `scripts/backtest.py` | 12671 | 2026-08-22T14:41:42-04:00 | a7bf1b36 2026-08-22 archive: store exact cents — rounding was manufacturing false candidates (#163) | no | B | audited-retain-producer |
| `scripts/calibration.py` | 5407 | 2026-08-18T20:56:51-04:00 | 7b7feec7 2026-08-18 shadow: log 92-93c survivors that reach 94-96c, and record the entry-timing work (#123) | no | B | audited-retain-producer |
| `scripts/entry_timing.py` | 3587 | 2026-08-18T20:56:51-04:00 | 7b7feec7 2026-08-18 shadow: log 92-93c survivors that reach 94-96c, and record the entry-timing work (#123) | no | B | audited-retain-producer |
| `scripts/gate_replay.py` | 12528 | 2026-08-22T13:48:52-04:00 | fa6bc832 2026-08-22 gate_replay: complete the funnel through SKIP reasons and submitted orders (#158) | no | B | audited-retain-producer |
| `scripts/kstat.py` | 8266 | 2026-08-23T19:26:59-04:00 | 7f84f642 2026-08-23 monitor: fees belong inside break-even (#174) | no | B | audited-retain-producer |
| `scripts/missed_pnl.py` | 5033 | 2026-08-19T18:03:17-04:00 | 77325fdd 2026-08-19 docs: record the Aug 19 session so a fresh context can pick it up (#135) | no | B | audited-retain-producer |
| `scripts/no_60d.py` | 8390 | 2026-08-17T17:15:22-04:00 | d7de51d0 2026-08-17 docs: replace WR-only $100 sizing gate with balance-based rule (#103) | no | B | audited-move M02 |
| `scripts/reconcile.py` | 14792 | 2026-08-22T14:30:51-04:00 | 66765463 2026-08-22 fix two defects found by external audit: NO fills dropped, depth gate stale (#161) | no | B | audited-retain-producer |
| `scripts/sizing.py` | 4069 | 2026-08-17T17:15:22-04:00 | d7de51d0 2026-08-17 docs: replace WR-only $100 sizing gate with balance-based rule (#103) | no | B | audited-move M02 |
| `scripts/vol_bucket_test.py` | 3631 | 2026-08-18T11:57:38-04:00 | ef1dfda6 2026-08-18 research: pre-registered volatility filter is refuted; record it with its command (#117) | no | B | audited-retain-producer |
| `scripts/verify.py` | 42113 | 2026-08-24T18:05:16-04:00 | 1b25ca01 2026-08-24 fix: kstat should report ET, not UTC — correcting my own B3 change (#200) | no | B | audited-retain-operational |
| `scripts/xlist_arb.py` | 5360 | 2026-08-17T17:15:22-04:00 | 3fe6c5a9 2026-08-17 docs: record exhausted parallel-strategy search + cross-listing arb scanner (#101) | no | B | audited-move M12 |
| `settled_corrected.csv` | 4149 | 2026-07-23T01:43:23-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | UNKNOWN | excluded-polymarket |
| `stale_bands.csv` | 14995 | 2026-07-23T13:25:31-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M11 |
| `test_order_safety.py` | 37715 | 2026-08-24T12:22:34-04:00 | 584d0a85 2026-08-24 v5.17: extend the entry band to 88-89c on the YES side only (#183) | yes | A | audited-retain-live |
| `trade_history.py` | 6876 | 2026-08-21T11:32:29-04:00 | UNTRACKED | no | B | audited-move M04 |
| `xvenue_candidates.csv` | 8870251 | 2026-07-23T13:13:27-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M12 |
| `xvenue_side_by_side.csv` | 8390 | 2026-07-23T13:18:49-04:00 | 6a5447ee 2026-07-31 Complete end-to-end audit: 6 fixes to late-certainty + hard halt on old strat | no | E | audited-move M12 |
| `/Users/chrisgarceau/Downloads/Finance/Kalshi (1).txt` | 1675 | 2026-07-26T16:03:13-04:00 | N/A (outside Git) | no | E | audited-sensitive-retain |
| `/Users/chrisgarceau/Downloads/Finance/Kalshi Printer.txt` | 1679 | 2026-07-26T00:40:53-04:00 | N/A (outside Git) | no | E | audited-sensitive-retain |
| `/Users/chrisgarceau/Downloads/Finance/kalshi (2).txt` | 1675 | 2026-07-26T16:03:24-04:00 | N/A (outside Git) | no | E | audited-sensitive-retain |
| `/Users/chrisgarceau/Downloads/Finance/kalshi.txt` | 1675 | 2026-07-26T16:02:18-04:00 | N/A (outside Git) | no | E | audited-sensitive-retain |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_optimizer.xlsx` | 12409 | 2026-04-01T19:45:47-04:00 | N/A (outside Git) | no | D | audited-move M11 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (1).py` | 142444 | 2026-07-21T16:54:37-04:00 | N/A (outside Git) | no | D | audited-move M11 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (2).py` | 170233 | 2026-07-21T21:46:56-04:00 | N/A (outside Git) | no | D | audited-move M11 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (3).py` | 170233 | 2026-07-21T21:48:08-04:00 | N/A (outside Git) | no | D | audited-delete X02 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (4).py` | 178423 | 2026-07-22T16:40:56-04:00 | N/A (outside Git) | no | D | audited-move M11 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (5).py` | 178423 | 2026-07-22T16:42:08-04:00 | N/A (outside Git) | no | D | audited-delete X03 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (6).py` | 178423 | 2026-07-22T16:52:34-04:00 | N/A (outside Git) | no | D | audited-delete X03 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge (7).py` | 178423 | 2026-07-22T16:52:34-04:00 | N/A (outside Git) | no | D | audited-delete X03 |
| `/Users/chrisgarceau/Downloads/Finance/kalshi_weather_edge.py` | 94805 | 2026-07-21T11:35:48-04:00 | N/A (outside Git) | no | D | audited-delete X01 |

## GitHub and infrastructure surface

These are surfaces rather than repository files, so they are not fabricated as file rows.

- Repository: `chrisgarceau6-dev/kalshi-trader`, default branch `main`, public.
- Workflows: Archive Candle Data, Clean-Day Measurement, Daily P&L Summary, Dashboard Keepalive, and Late-Certainty Trader; all reported active.
- Open pull requests: none returned.
- Action-secret names only: `COPY_EMAIL_FROM`, `COPY_EMAIL_PASSWORD`, `COPY_EMAIL_TO`, `GH_DISPATCH_TOKEN`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `NTFY_TOPIC`. Values were not requested or read.
- Actions policy: enabled; all actions allowed; SHA pinning not required; default token permission read; workflow tokens cannot approve pull-request reviews.
- Render manifest: service `kalshi-dashboard`, build installs Flask/Requests/Cryptography, start command `python kalshi_dashboard.py`; manifest env names are `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `DASH_TOKEN`, and `GH_READ_TOKEN`.
- Dashboard keepalive targets `https://polymarket-monitor2.onrender.com`; on 2026-08-24 that endpoint returned HTTP 401 with a Kalshi page title, while the stale `kalshi-dashboard.onrender.com` comment target returned HTTP 503.
- GitHub reported 110 branch refs, 17,382 Actions artifacts in repository metadata, and the latest 100 cache records requested by the audit endpoint. The artifacts and cache bodies were not downloaded.

Reproduce all GitHub metadata (secret names only) and endpoint checks with:

```bash
gh repo view --json nameWithOwner,defaultBranchRef,isPrivate
gh api --paginate repos/{owner}/{repo}/branches --jq '.[].name' | wc -l
gh workflow list --all
gh pr list --state open --json number,title,headRefName,baseRefName
gh secret list --app actions
gh api repos/{owner}/{repo}/actions/permissions
gh api repos/{owner}/{repo}/actions/permissions/workflow
gh api repos/{owner}/{repo}/actions/artifacts --jq '.total_count'
gh cache list --limit 100 --json id,key,sizeInBytes,createdAt,lastAccessedAt | jq length
curl -sS -o /dev/null -w '%{http_code}\n' https://polymarket-monitor2.onrender.com
curl -sS -o /dev/null -w '%{http_code}\n' https://kalshi-dashboard.onrender.com
```

## UNKNOWN

No inventory row remains unclassified. The former ambiguous rows were resolved from their producers/history: `backtest_us_portfolio.png` and `settled_corrected.csv` are excluded Polymarket artifacts; `chatgpt_review_prompt.txt` is a Kalshi review handoff proposed for M13.

Runtime secret values, Render's deployed environment values, and Actions artifact/cache contents were deliberately not inspected. They are outside the authorized content surface; their names/metadata were inventoried where permitted.
