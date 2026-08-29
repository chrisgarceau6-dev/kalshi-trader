#!/usr/bin/env bash
# Live z-gate watch. Tracks by TIMESTAMP, not by trying to catch each run as it appears
# — the previous version polled --limit 10 every 15 min and silently missed runs while
# the machine slept, logging 24 of 99. An undercounting watcher can also miss an ALERT,
# which is the part that actually matters.
cd ~/pm
LAST="${1:-2026-08-27T15:48:00Z}"
S=0; P=0; F=0; E=0; R=0; FAILS=0
while true; do
  NEW=$(gh run list --workflow=late_certainty.yml --limit 200 --json databaseId,conclusion,createdAt \
        --jq "[.[]|select(.createdAt>\"$LAST\" and .conclusion!=null and .conclusion!=\"\")]|sort_by(.createdAt)|.[]|\"\(.createdAt) \(.conclusion) \(.databaseId)\"" 2>/dev/null)
  [ -z "$NEW" ] && { sleep 600; continue; }
  while read -r TS CONC ID; do
    [ -z "$ID" ] && continue
    LAST="$TS"
    [ "$CONC" = "cancelled" ] && continue
    if [ "$CONC" != "success" ]; then
      FAILS=$((FAILS+1))
      [ "$FAILS" -ge 2 ] && echo "ALERT $FAILS consecutive failed runs (latest $ID $CONC)"
      continue
    fi
    FAILS=0
    L=$(gh run view "$ID" --log 2>/dev/null)
    echo "$L" | grep -q "ZGATE-" || continue
    s=$(echo "$L"|grep -c ZGATE-SKIP); p=$(echo "$L"|grep -c ZGATE-PASS)
    f=$(echo "$L"|grep -c "\[EXEC\]"); e=$(echo "$L"|grep -cE "Traceback|EXECUTION HALT|CRASH FILL|user_not_found")
    S=$((S+s)); P=$((P+p)); F=$((F+f)); E=$((E+e)); R=$((R+1))
    [ "$e" -gt 0 ] && { echo "ALERT errors in run $ID:"; echo "$L"|grep -oE "(Traceback|EXECUTION HALT|CRASH FILL|user_not_found).{0,80}"|head -3; }
    if [ $((R % 16)) -eq 0 ]; then
      T=$((S+P)); RATE=$(python3 -c "print(f'{$S/max($T,1)*100:.0f}%')")
      echo "zgate watch $(date -u +%H:%MZ): $R runs | $T decisions | blocked $S ($RATE) | fills $F | errors $E | through $LAST"
    fi
  done <<< "$NEW"
  sleep 600
done
