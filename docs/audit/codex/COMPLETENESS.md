# Completeness gate

Snapshot: 2026-08-24, current `main`. This closes the charter gate against the corrected census, not against Session 1 conclusions.

## Denominator

The table in `INVENTORY.md` has three corrections beyond its original rows:

1. `.claude-flow/sessions/current.json`, which was tracked at the starting revision but omitted. Its pre-existing worktree deletion is preserved; the row uses the `HEAD` blob and commit metadata.
2. `scripts/verify.py`, created during the audit and now part of current operational measurement.
3. One explicit grouped `_tmp_*.csv` row. The producer references prove these are excluded Polymarket wallet-screen exports, but the group remains in the table so the exclusion cannot disappear from the denominator.

Audit work papers created inside `docs/audit/claude/` and `docs/audit/codex/` after the starting walk are excluded as audit output. Including the inventory itself in the audited subject would create a moving, self-referential census. The charter, which predates and governs the work, remains an individual row.

Run:

```bash
rows=$(awk -F'|' '/^\| `/ {n++} END {print n+0}' docs/audit/codex/INVENTORY.md)
tmp=$(find . -maxdepth 1 -type f -name '_tmp_*.csv' | wc -l | tr -d ' ')
printf 'table_rows=%s represented_paths=%s\n' "$rows" "$((rows - 1 + tmp))"
```

Current result:

```text
table_rows=263 represented_paths=3800
```

The subtraction replaces the one grouped glob row with the files it represents. This is a coverage count, not a claim that charter-excluded Polymarket data was audited as Kalshi evidence.

## Status semantics

- `audited-retain-live` — reachable from the current live workflow import/resource graph and checked against current main.
- `audited-retain-operational`, `audited-retain-producer`, `audited-retain-evidence`, `audited-retain-research`, `audited-retain-artifact`, `audited-retain-tooling` — classified and retained for the named role.
- `audited-sensitive-retain` — classified as credential/config material; values were not inventoried and the file must not be moved into a tracked archive.
- `audited-move Mnn` / `audited-delete Xnn` — checked and linked to one approval row in `MOVES.md`. Nothing has been executed.
- `excluded-polymarket` / `excluded-polymarket-generated` — classified and explicitly excluded by charter, rather than silently absent.
- `UNKNOWN` — inspected but unclassifiable. No row currently has this status.
- `unaudited` — not checked. The final gate fails if this appears anywhere in a table row.

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

The command exits successfully with `BAD 0`. Status counts are derived by the command rather than copied here, so the gate cannot become stale when Chris approves individual manifest rows.

### 2. No duplicate table keys

```bash
awk -F'|' '/^\| `/ {p=$2; gsub(/^ +`|` +$/, "", p); print p}' \
  docs/audit/codex/INVENTORY.md | sort | uniq -d | awk '{bad=1; print} END {exit bad}'
```

No output and exit zero means every table key is unique.

### 3. Every individual row resolves

The grouped glob and the tracked-but-worktree-deleted Ruflo pointer need explicit handling; every other row must exist on disk.

```bash
while IFS= read -r p; do
  case "$p" in
    '_tmp_*.csv (grouped; 3,538 files)') continue ;;
    '.claude-flow/sessions/current.json')
      git cat-file -e 'HEAD:.claude-flow/sessions/current.json' || exit 1
      continue
      ;;
  esac
  test -e "$p" || { echo "MISSING $p"; exit 1; }
done < <(awk -F'|' '/^\| `/ {p=$2; gsub(/^ +`|` +$/, "", p); print p}' \
  docs/audit/codex/INVENTORY.md)
```

No output and exit zero means each individual key resolves to the current filesystem or the explicitly identified `HEAD` blob.

### 4. Grouped exclusion is complete and correctly attributed

```bash
find . -maxdepth 1 -type f -name '_tmp_*.csv' | wc -l
find . -maxdepth 1 -type f -name '_tmp_*.csv' -exec stat -f '%z' {} + |
  awk '{s+=$1} END {print s}'
git ls-files '_tmp_*.csv' | wc -l
git grep -n '_tmp_.*csv' -- strategy_dissect.py backtest_us_portfolio.py
```

The first three outputs reproduce the grouped row's file count, bytes, and untracked status. The final command identifies the excluded Polymarket producers. This converts the former silent omission into an asserted classification.

### 5. Every proposed action has an approval row

```bash
comm -3 \
  <(awk -F'|' '/^\| `/ && $8 ~ /audited-(move|delete)/ {
       s=$8; gsub(/^ +| +$/, "", s); sub(/^audited-(move|delete) /, "", s); print s
     }' docs/audit/codex/INVENTORY.md | sort -u) \
  <(awk -F'|' '/^\| [MX][0-9][0-9] / {
       id=$2; gsub(/^ +| +$/, "", id); print id
     }' docs/audit/codex/MOVES.md | sort -u)
```

No output means no candidate can be moved or deleted without a corresponding line item in the approval manifest, and no manifest item lacks inventoried sources.

## Gate result

**PASS:** zero `unaudited`, zero `UNKNOWN`, no duplicate keys, every individual row resolves, the large omitted glob is explicitly represented and charter-classified, and every proposed action maps to an approval ID. No move, deletion, or trading-path edit was performed.
