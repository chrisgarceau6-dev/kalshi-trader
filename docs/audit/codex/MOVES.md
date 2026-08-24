# Executed moves and deletions — approval and execution manifest

Snapshot: 2026-08-24. Chris approved all 16 rows: 13 move rows and 3 byte-identical deletion rows. Every approved row was then executed in the commit containing this document. No unapproved path was moved or deleted.

The live path was traced again after the changes through PR #200. The v5.17 band repair, unified dynamic depth, resized emergency brakes, corrected harness, dashboard, `kstat`, and `scripts/verify.py` are current and are not candidates below.

## Risk scale

- **R1 — record-only:** no trading or measurement code should depend on the source path.
- **R2 — research/measurement:** no live orders change, but commands, imports, or evidence paths must be updated after the move.
- **R3 — trading availability:** not invoked by production, but removing the present path disables a manually runnable real-money route.
- **R4 — exposure:** would alter live risk. There are no R4 pruning proposals.

## Reproduction commands

Run from the repository root. These commands establish current entrypoints, references, provenance, exact duplicates, and the Polymarket exclusion; they do not mutate anything.

```bash
# Current production/measurement entrypoints.
git log -1 --oneline --decorate
for f in .github/workflows/*.yml; do
  echo "### $f"
  rg -n 'python |uses:|cron:|run:|workflow:' "$f"
done
rg -n 'command:|kalshi_dashboard.py' render.yaml
rg -n '^## Collecting right now|^## Next actions|^## Open threads' CLAUDE.md

# For any proposed source PATH: tracked state, references outside audit papers,
# and last committed provenance.
git ls-files --error-unmatch PATH
git grep -n -F 'PATH' -- ':!docs/audit/codex/**' ':!docs/audit/claude/**'
git log -1 --date=iso-strict --format='%H%n%ad%n%s' -- PATH

# Legacy root programs are not invoked by a workflow or Render.
rg -n 'backtest_(ablation|acceleration|ask_floor|blackout_hours|breakout|btcd|cross_asset|filter_audit|hourly|multistrike|series_pause|ultralate|vol_filter)' \
  .github/workflows render.yaml || true

# Producer/output edges that must travel together.
rg -n 'backtest_.*\.csv|kalshi_.*\.csv|xvenue_.*\.csv|features_cache|spot_.*\.json' \
  --glob '*.py' --glob '*.sh' .

# Retained weather canonicals still reproduce the three approved hash values.
# The five duplicate sources were compared byte-for-byte to these exact files
# immediately before deletion; the tests below now prove those sources are gone.
shasum -a 256 archive/research/weather/kalshi_weather_edge.py \
  "archive/research/weather/external-finance/kalshi_weather_edge (2).py" \
  "archive/research/weather/external-finance/kalshi_weather_edge (4).py"
for p in \
  "$HOME/Downloads/Finance/kalshi_weather_edge.py" \
  "$HOME/Downloads/Finance/kalshi_weather_edge (3).py" \
  "$HOME/Downloads/Finance/kalshi_weather_edge (5).py" \
  "$HOME/Downloads/Finance/kalshi_weather_edge (6).py" \
  "$HOME/Downloads/Finance/kalshi_weather_edge (7).py"; do
  test ! -e "$p"
done

# The excluded temporary CSVs are Polymarket wallet-screen outputs, not Kalshi
# evidence. This is why they are represented by one explicit excluded census row.
find . -maxdepth 1 -type f -name '_tmp_*.csv' | wc -l
find . -maxdepth 1 -type f -name '_tmp_*.csv' -exec stat -f '%z' {} + |
  awk '{s+=$1} END {print s}'
git grep -n '_tmp_.*csv' -- strategy_dissect.py backtest_us_portfolio.py

# M05 was a dormant second real-money route, not merely an obsolete helper.
# Capture its command and verify the route had never run before archiving it.
sed -n '1,220p' archive/operations/retired/heartbeat.sh
pgrep -fl '[h]eartbeat.sh|late_certainty_trader.py' || true
test ! -e /tmp/kalshi_heartbeat.log
```

## Executed moves

All destinations are under `archive/` and preserve source material. Chris approved each ID independently before execution.

| ID | source(s) | destination | evidence that it is inert now | risk | decision |
|---|---|---|---|:---:|---|
| M01 | `.DS_Store` | `archive/metadata/root.DS_Store` | Finder metadata; no code, workflow, documentation, or evidence reader references it. | R1 | EXECUTED |
| M02 | `backtest_85c_btc.csv`, `backtest_ablation.py`, `backtest_ablation_raw.csv`, `backtest_ablation_spot.csv`, `backtest_acceleration.py`, `backtest_acceleration.csv`, `backtest_ask_floor.py`, `backtest_ask_floor.csv`, `backtest_blackout_hours.py`, `backtest_blackout_hours.csv`, `backtest_breakout.py`, `backtest_breakout.csv`, `backtest_btcd.py`, `backtest_buckets.csv`, `backtest_buckets_60d.csv`, `backtest_commodities_filter.csv`, `backtest_commodities_nofilter.csv`, `backtest_cross_asset.py`, `backtest_cross_asset.csv`, `backtest_filter_audit.py`, `backtest_filter_audit.csv`, `backtest_hourly.py`, `backtest_multistrike.py`, `backtest_no_filter.csv`, `backtest_series_pause.py`, `backtest_ultralate.py`, `backtest_vol_filter.py`, `scripts/no_60d.py`, `scripts/sizing.py` | `archive/research/legacy-root-backtests/`, preserving basenames | None is invoked by a current workflow or Render. The untracked programs are the scratch producers identified in the Tier C audit. `no_60d.py` and `sizing.py` still encode the retired stake and fill model. The ablation inputs/outputs travel with their consumers. | R2 | EXECUTED |
| M03 | `backtest_commodities.py`, `backtest_longshot.py`, `backtest_wti_optimize.py` and `backtest_longshot.csv` / `backtest_longshot_oos.csv` | `archive/research/legacy-root-backtests/tracked/` | Tracked historical producers, not current entrypoints. WTI is paused and longshot is in the graveyard. The move preserves their git content while removing executable-looking programs from the root. | R2 | EXECUTED |
| M04 | `trade_history.py`, `backtest_run.log`, `coinbase_fetch.log`, `crypto15m.log`, `expand_candles.log`, `kalshi_pnl_chart.png`, `kalshi_settlements.json`, `kalshi_snapshot.json`, `kalshi_trade_history.json`, `losses_report.pdf` | `archive/diagnostics/legacy/` | No current workflow, Render command, or current operational table reads them. They are untracked point-in-time exports or old diagnostics. `certainty.log` and `certainty_state.json` are deliberately excluded from this row because current trader code names them. | R2 | EXECUTED |
| M05 | `heartbeat.sh` | `archive/operations/retired/heartbeat.sh` | The untracked daemon runs `late_certainty_trader.py` locally every 60 seconds. It bypasses the workflow concurrency group and, from an empty local state file, could double-trade against the Actions instance. Before execution, `pgrep` found no heartbeat/trader process and `/tmp/kalshi_heartbeat.log` did not exist, so there was no evidence it had ever run. Archiving disables this dormant second real-money route. | **R3** | EXECUTED |
| M06 | `research/capture/` | `archive/research/refuted/capture/` | The capture/EXTRA experiment is superseded. Current `scripts/reconcile.py` mentions `audit2.py` only as historical context and does not import it. | R2 | EXECUTED |
| M07 | `research/hourly_crypto/` including its compressed archives | `archive/research/refuted/hourly_crypto/` | The standalone hourly strategy is in the graveyard. The producer and inputs must move together so the refutation remains reproducible after updating the documented command. | R2 | EXECUTED |
| M08 | `research/kalshi_incentives/` | `archive/research/refuted/kalshi_incentives/` | Incentives are in “not currently pursued”; nothing imports the folder. Move the README, scripts, and surviving logs as one evidence package. | R2 | EXECUTED |
| M09 | `research/loss_cooldown/` | `archive/research/refuted/loss_cooldown/` | Cooldown and post-loss size-up are in the graveyard. Current emergency-brake evidence is independently retained in `docs/audit/claude/replay_loss_limit.py`. | R2 | EXECUTED |
| M10 | `research/projection/` | `archive/research/legacy/projection/` | No current workflow, current experiment, or current documented command names these capacity/forward scripts. | R2 | EXECUTED |
| M11 | `kalshi_weather_edge.py`, `backtest_trades.csv`, `kalshi_calib.csv`, `kalshi_calibration.csv`, `stale_bands.csv`, the unique Finance weather versions retained after X01–X03, and `~/Downloads/Finance/kalshi_optimizer.xlsx` | `archive/research/weather/` with `external-finance/` for former Downloads files | Weather is closed/not pursued. The root file is the oldest tracked implementation; the newer unique Finance versions are otherwise detached from git. Moving the whole provenance chain preserves rather than deletes the evidence. | R2 | EXECUTED |
| M12 | `kalshi_promo_hedge.py`, `scripts/xlist_arb.py`, `band_arb_live.csv`, `band_arb_sets.csv`, `liquidity_reward_candidates.csv`, `rewards_universe.csv`, `xvenue_candidates.csv`, `xvenue_side_by_side.csv`, `vegas_cron.py` | `archive/research/market-structure/` | Promo hedging is unrelated to the running strategy; cross-listing arb and reward farming are not currently pursued. The xvenue outputs and `vegas_cron.py` are Kalshi cross-venue evidence and move with this market-structure family rather than being excluded as Polymarket. No live or monitoring entrypoint imports these files. | R2 | EXECUTED |
| M13 | `chatgpt_review_prompt.txt` | `archive/reviews/2026-08-legacy-second-opinion.txt` | Point-in-time review prompt describing retired WTI/multistrike settings; no program reads it. | R1 | EXECUTED |

### Execution safeguards

- Archive additions were staged from the exact `executed-move` inventory paths, not with a broad `git add archive/`.
- The incentive JSON and `.claude-flow/` ignore rules were repointed to `archive/research/refuted/kalshi_incentives/` in the move commit. Files already ignored by `*.log` and `kalshi_snapshot.json` remain deliberately untracked after their local move; reproduce the decision with `git check-ignore -v archive/diagnostics/legacy/*.log archive/diagnostics/legacy/kalshi_snapshot.json archive/research/refuted/kalshi_incentives/*.log`.
- `archive/operations/retired/heartbeat.sh` is deliberately newly tracked. Its R3 classification and dormant double-trading route are preserved in M05.

### Explicit retain decisions

These are recorded because their names might otherwise look pruneable:

- `late_certainty_trader.py`, `kalshi_auth.py`, `test_order_safety.py`, all current workflows, `daily_summary.py`, `kalshi_dashboard.py`, `render.yaml`, and `requirements.txt`: current production/operations.
- `scripts/archive_candles.py`, `scripts/backtest.py`, `scripts/calibration.py`, `scripts/entry_timing.py`, `scripts/gate_replay.py`, `scripts/kstat.py`, `scripts/missed_pnl.py`, `scripts/reconcile.py`, `scripts/verify.py`, and `scripts/vol_bucket_test.py`: current measurement or surviving evidence producers.
- `research/perp_overlay/` and `research/top5/`: MOM3 and time-weighted sizing remain open/current research.
- `data/candles/`, `data/gatelog/`, and the perp spot/cache inputs: primary or producer-bound evidence. The approved hourly evidence moved with M07.
- `.env`, `kalshi_key.pem`, and the four Finance text files classified as key material: sensitive; not archive candidates and never to be committed into `archive/`.
- `.claude-flow/` and `.claude/`: current agent/tooling state. Some files are old, but the worktree shows present churn, so “inert” is not established.
- `certainty_state.json` and `certainty.log`: named by the trader even though production state is transported through Actions cache/artifacts.
- `docs/websocket_spec.md`: deliberately unwired design evidence, not dead code.
- `_tmp_*.csv`: explicitly excluded Polymarket wallet-screen output. It is represented in the completeness gate but is outside this Kalshi pruning lane.

## Executed deletions — byte-identical only

Every deletion kept one byte-identical canonical copy. The hash and `cmp` checks were rerun immediately before execution; any mismatch would have cancelled that row.

| ID | delete | canonical copy retained | equality evidence | risk | decision |
|---|---|---|---|:---:|---|
| X01 | `~/Downloads/Finance/kalshi_weather_edge.py` | `archive/research/weather/kalshi_weather_edge.py` | SHA-256 for both is `36c1e28c0f50bd46abf2e1e97be0e0c33fef435d0585dee9a9484f811f688304`. | R1 | EXECUTED |
| X02 | `~/Downloads/Finance/kalshi_weather_edge (3).py` | `archive/research/weather/external-finance/kalshi_weather_edge (2).py` | SHA-256 for both is `ebdd79c6fea2b6ab70c3ed9255e93e02db5036a71dfcc2976a49dcfe27f059a9`. | R1 | EXECUTED |
| X03 | `~/Downloads/Finance/kalshi_weather_edge (5).py`, `(6).py`, `(7).py` | `archive/research/weather/external-finance/kalshi_weather_edge (4).py` | SHA-256 for all four is `be3297b2a7d5060dce11ba4f7cd15799f23fa682e5034f0b83d6c6c70795073a`. | R1 | EXECUTED |

No other deletion was executed. Unique logs, outputs, scripts, and scratch files remain move-or-retain only. The byte-identical `_tmp_*.csv` pairs were deliberately not listed because the producing programs establish that the entire glob belongs to excluded Polymarket work.
