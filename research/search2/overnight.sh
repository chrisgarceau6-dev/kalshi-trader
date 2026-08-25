#!/bin/bash
# Overnight runner for the strategy-2 search (steps 3-5).
#
# Sequencing matters: the pull shares Kalshi API credentials with the LIVE TRADER.
# Everything here is paced with sleeps and runs one series at a time so the trader's
# calls are never starved. If a step fails the script continues — a partial archive is
# still scannable, and pull.py is resumable.
set -u
cd "$(dirname "$0")/../.." || exit 1
LOG_DIR="${1:-/private/tmp/claude-501/-Users-chrisgarceau/54450243-7186-435f-8d94-8a8ec785be97/scratchpad}"
OUT="$LOG_DIR/overnight.log"
say() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$OUT"; }

say "=== strategy-2 overnight run starting ==="

# 1. Wait for any pull already in flight.
while pgrep -f "search2/pull.py" >/dev/null; do sleep 20; done
say "initial pull finished: $(ls research/search2/data 2>/dev/null | wc -l | tr -d ' ') series on disk"

# 2. Widen the archive. Resumable, so already-pulled series are skipped instantly.
for N in 30 45 60; do
  say "widening pull to top $N by volume..."
  python3 research/search2/pull.py --top "$N" >>"$LOG_DIR/pull.log" 2>&1
  say "  now $(ls research/search2/data 2>/dev/null | wc -l | tr -d ' ') series on disk"
  # Breathe between waves so the trader always has API headroom.
  sleep 30
done

# 3. Full scan over everything collected.
say "running full scan..."
python3 research/search2/scan.py --min-n 400 --top 20 > "$LOG_DIR/scan_full.txt" 2>&1
say "  scan written to scan_full.txt"

# 4. Holdout scan — the same slices on the most recent 30% of clusters only.
#    A lead that survives in-sample but dies here is noise, and this is the cheapest
#    possible version of the out-of-sample discipline v5.17 needed.
say "running holdout scan (most recent 30% of clusters)..."
python3 research/search2/scan.py --min-n 200 --top 20 --holdout 0.3 \
  > "$LOG_DIR/scan_holdout.txt" 2>&1
say "  holdout written to scan_holdout.txt"

# 5. Per-series price detail for whatever looked best.
say "running series x price detail..."
python3 research/search2/scan.py --by series price --min-n 300 --top 40 \
  > "$LOG_DIR/scan_series_price.txt" 2>&1

say "=== done. $(ls research/search2/data 2>/dev/null | wc -l | tr -d ' ') series archived ==="
