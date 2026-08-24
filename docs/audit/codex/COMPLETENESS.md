# Completeness gate

Snapshot: 2026-08-24 after execution of the approved M01-M13 and X01-X03 rows. This closes the charter gate against generated filesystem and Git inputs, not against a curated Session 1 list.

## Denominator corrections

The current inventory adds eight rows beyond the original table:

1. `.claude-flow/sessions/current.json`, tracked at the starting revision but omitted. Its pre-existing worktree deletion is preserved; the row uses the `HEAD` blob.
2. `scripts/verify.py`, created during the audit and now current operational measurement.
3. One explicit `_tmp_*.csv` group. Its producers identify it as charter-excluded Polymarket wallet output.
4. One dynamic `docs/audit/{claude,codex}/**` group for post-census audit work papers.
5. One exact-manifest group for the other tracked Polymarket files.
6. `xvenue_candidates.csv`, a Kalshi/Polymarket cross-venue research output.
7. `xvenue_side_by_side.csv`, the corresponding Kalshi-side market snapshot.
8. `vegas_cron.py`, a Kalshi/Prophet Exchange arbitrage tool using Vegas odds as a reference. It was incorrectly included in the Polymarket exclusion manifest and is now an individual M12 market-structure row.

The two xvenue files and `vegas_cron.py` are real Kalshi evidence. They carry M12 in `archive/research/market-structure/` and are not hidden inside an exclusion.

```bash
awk -F'|' '/^\| `/ {n++} END {print "table_rows", n+0}' docs/audit/codex/INVENTORY.md
git ls-files | wc -l
```

The table currently has 268 rows. Row count alone is not the completeness proof; the tracked-file `comm` gate below is.

## Exact tracked Polymarket exclusion manifest

This list is exact, not a permissive filename rule. A future tracked file is not automatically excluded: it makes Gate 5 fail until it receives an inventory row or is deliberately added here.

<!-- POLYMARKET_TRACKED_BEGIN -->
```text
all_wallets_master.txt
backtest_us_portfolio.py
backtest_us_portfolio.txt
combo_optimizer_results.csv
exhaust_final.txt
exhaust_progress.txt
exhaust_results.csv
fgd_s00.csv
fgd_s03.csv
full_rescreen_results.txt
harvested_wallets.txt
mega_harvest_progress.txt
mm_backtest.csv
oracle_setup.sh
poli_s00.csv
poli_s02.csv
poli_s03.csv
poli_weather_positions.csv
poly_backtest_no_0.75-0.85.png
poly_calibration.csv
poly_consistency.csv
poly_copy_backtest.csv
poly_scan.csv
poly_us_portfolio.csv
portfolio.csv
smart_wallets.csv
smart_wallets_v2.csv
smart_wallets_v3.csv
smoke.csv
snow_s00.csv
snow_s03.csv
strategy_dissect.py
validate_screener.py
vp_h4.csv
vp_h6.csv
vp_s03.csv
wallet_5point_results.csv
wallet_screen.csv
wallets_batch2.txt
wallets_candidates.txt
xrp_s00.csv
xrp_s03.csv
```
<!-- POLYMARKET_TRACKED_END -->

Reproduce the manifest count and tracked status without inspecting secret contents:

```bash
poly_exclusions() {
  sed -n '/^<!-- POLYMARKET_TRACKED_BEGIN -->$/,/^<!-- POLYMARKET_TRACKED_END -->$/p' \
    docs/audit/codex/COMPLETENESS.md | grep -vE '^<!--|^```|^$'
}
poly_exclusions | wc -l
poly_exclusions | while IFS= read -r p; do stat -f '%z' "$p"; done |
  awk '{s+=$1} END {print s}'
poly_exclusions | while IFS= read -r p; do
  stat -f '%m %Sm' -t '%Y-%m-%dT%H:%M:%S%z' "$p"
done | sort -n | sed -n '1p;$p'
comm -23 <(poly_exclusions | sort) <(git ls-files | sort)
```

The final command produces no output: every explicit exclusion is tracked.

## Status semantics

- `audited-retain-live` — reachable from the current live workflow import/resource graph.
- `audited-retain-*` — classified and retained for the suffix role.
- `audited-sensitive-retain` — credential/config material; values are outside the authorized content surface and must not be moved into tracked `archive/`.
- `executed-move Mnn` / `executed-delete Xnn` — linked to one Chris-approved row in `MOVES.md`; the move or verified duplicate deletion has been executed.
- `excluded-polymarket`, `excluded-polymarket-generated`, `excluded-polymarket-tracked` — explicitly charter-excluded.
- `excluded-audit-work-product` — audit files created after the starting census; generated dynamically from the two audit directories.
- `UNKNOWN` — inspected but unclassifiable. No row currently has this status.
- `unaudited` — not checked. Gate 1 fails if this appears in a table row.

## Mechanical gates

### 1. Zero unaudited and zero unclassified rows

```bash
awk -F'|' '
  /^\| `/ {
    rows++
    s=$8; gsub(/^ +| +$/, "", s)
    counts[s]++
    if (s == "unaudited" || s == "UNKNOWN" || s == "") bad++
  }
  END {
    for (s in counts) print counts[s], s
    print "ROWS", rows+0
    print "BAD", bad+0
    exit(bad != 0)
  }
' docs/audit/codex/INVENTORY.md | sort
```

Required result: `BAD 0` and exit zero.

### 2. No duplicate table keys

```bash
awk -F'|' '/^\| `/ {p=$2; gsub(/^ +`|` +$/, "", p); print p}' \
  docs/audit/codex/INVENTORY.md | sort | uniq -d | awk '{bad=1; print} END {exit bad}'
```

Required result: no output and exit zero.

### 3. Every individual row resolves

```bash
while IFS=$'\t' read -r p s; do
  if [[ "$s" == executed-delete\ * ]]; then
    test ! -e "$p" || { echo "DELETE_SURVIVED $p"; exit 1; }
    continue
  fi
  case "$p" in
    '_tmp_*.csv (grouped; 3,538 files)'|\
    'docs/audit/{claude,codex}/** (grouped audit work papers)'|\
    'tracked Polymarket files (grouped charter exclusion)') continue ;;
    '.claude-flow/sessions/current.json')
      git cat-file -e 'HEAD:.claude-flow/sessions/current.json' || exit 1
      continue
      ;;
  esac
  test -e "$p" || { echo "MISSING $p"; exit 1; }
done < <(awk -F'|' '/^\| `/ {
  p=$2; gsub(/^ +`|` +$/, "", p)
  s=$8; gsub(/^ +| +$/, "", s)
  print p "\t" s
}' \
  docs/audit/codex/INVENTORY.md)
```

Required result: no output and exit zero.

### 4. Generated `_tmp_*.csv` exclusion is complete and attributed

```bash
find . -maxdepth 1 -type f -name '_tmp_*.csv' | wc -l
find . -maxdepth 1 -type f -name '_tmp_*.csv' -exec stat -f '%z' {} + |
  awk '{s+=$1} END {print s}'
git ls-files '_tmp_*.csv' | wc -l
git grep -n '_tmp_.*csv' -- strategy_dissect.py backtest_us_portfolio.py
```

These reproduce the grouped row and its excluded Polymarket producers.

### 5. Every tracked file is represented or explicitly excluded

```bash
poly_exclusions() {
  sed -n '/^<!-- POLYMARKET_TRACKED_BEGIN -->$/,/^<!-- POLYMARKET_TRACKED_END -->$/p' \
    docs/audit/codex/COMPLETENESS.md | grep -vE '^<!--|^```|^$'
}

comm -23 \
  <(git ls-files | sort) \
  <(
    {
      awk -F'|' '/^\| `/ {
        p=$2; gsub(/^ +`|` +$/, "", p)
        if (p !~ /\(grouped/ && p !~ /^\//) print p
      }' docs/audit/codex/INVENTORY.md
      git ls-files 'docs/audit/claude/**' 'docs/audit/codex/**'
      poly_exclusions
    } | sort -u
  )
```

Required result: no output. Unlike Gate 1, this begins with `git ls-files`; a missing inventory row cannot be concealed by the table it is checking.

### 6. Every executed action has an approval row

```bash
comm -3 \
  <(awk -F'|' '/^\| `/ && $8 ~ /executed-(move|delete)/ {
       s=$8; gsub(/^ +| +$/, "", s); sub(/^executed-(move|delete) /, "", s); print s
     }' docs/audit/codex/INVENTORY.md | sort -u) \
  <(awk -F'|' '/^\| [MX][0-9][0-9] / {
       id=$2; gsub(/^ +| +$/, "", id); print id
     }' docs/audit/codex/MOVES.md | sort -u)
```

Required result: no output.

## Gate result

**PASS requires all six commands to exit zero:** zero `unaudited`, zero `UNKNOWN`, unique keys, every retained or moved individual row resolves, every executed deletion is absent, every tracked path is represented or exactly excluded, and every executed action maps to its approval ID. The gate does not alter the trading path.
