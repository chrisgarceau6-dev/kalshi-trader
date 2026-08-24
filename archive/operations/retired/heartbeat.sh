#!/bin/bash
# Local trader daemon — runs late_certainty_trader.py directly on Mac.
# Bypasses GitHub Actions entirely: no GitHub infrastructure dependency.
# Runs every 60s; state persists in certainty_state.json on disk.

SCRIPT_DIR="/Users/chrisgarceau/pm"
LOG="/tmp/kalshi_heartbeat.log"

export KALSHI_API_KEY_ID="1693e08f-c8ec-4a38-a8e6-5d505bd3a9f5"
export KALSHI_PRIVATE_KEY_PATH="/Users/chrisgarceau/.kalshi/private_key.pem"
export COPY_EMAIL_FROM="chrisgarceau6@gmail.com"
export COPY_EMAIL_TO="chrisgarceau6@gmail.com"
export COPY_EMAIL_PASSWORD=""

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG"; }

log "trader daemon started (pid $$)"

cd "$SCRIPT_DIR"

while true; do
    /usr/bin/python3 late_certainty_trader.py --once >> /tmp/kalshi_trader.log 2>&1
    exit_code=$?
    if [ $exit_code -ne 0 ]; then
        log "trader exited non-zero (code=$exit_code)"
    fi
    sleep 60
done
