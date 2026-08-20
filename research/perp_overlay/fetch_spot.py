#!/usr/bin/env python3
"""Pull 1-min Coinbase closes for every series the 15M strategy trades.

Public market data only — no credentials. Cached per product so re-runs are cheap.
Used to price a perp-overlay hedge against the archived Kalshi entries.
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

OUT = os.path.dirname(os.path.abspath(__file__))
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD", "BNB-USD"]
START = int(datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc).timestamp())
END   = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp())
STEP  = 300 * 60          # 300 one-minute candles per request (API max)


def fetch(product):
    path = os.path.join(OUT, f"spot_{product}.json")
    have = {}
    if os.path.exists(path):
        have = {int(k): v for k, v in json.load(open(path)).items()}
    t, req_n, added = START, 0, 0
    while t < END:
        hi = min(t + STEP, END)
        # skip a window we already have densely covered
        want = range(t, hi, 60)
        if sum(1 for m in want if m in have) >= 0.95 * len(list(want)):
            t = hi
            continue
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
               f"?granularity=60&start={t}&end={hi}")
        for attempt in range(4):
            try:
                r = urllib.request.Request(url, headers={"User-Agent": "perp-overlay/1.0"})
                data = json.loads(urllib.request.urlopen(r, timeout=25).read())
                for row in data:
                    have[int(row[0])] = float(row[4])   # close
                    added += 1
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  {product} {t} failed: {e}", file=sys.stderr)
                time.sleep(1.5 * (attempt + 1))
        req_n += 1
        if req_n % 50 == 0:
            print(f"  {product}: {req_n} reqs, {len(have):,} minutes", flush=True)
        time.sleep(0.14)
        t = hi
    json.dump({str(k): v for k, v in sorted(have.items())}, open(path, "w"))
    print(f"{product}: {len(have):,} minutes cached -> {path}", flush=True)


if __name__ == "__main__":
    for p in (sys.argv[1:] or PRODUCTS):
        fetch(p)
