#!/usr/bin/env python3
"""Fetch 1-min spot OHLC from Coinbase for the six live underlyings.

Cache: data/.spot_cache/<PRODUCT>.csv  (ts,open,high,low,close,volume), gitignored.
Idempotent: re-running only fetches ts ranges not already cached.
"""
import csv, os, sys, time, json, urllib.request, urllib.error
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/pm")
CACHE = os.path.join(ROOT, "data", ".spot_cache")
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "BNB-USD", "XRP-USD"]
GRAN = 60
MAXC = 300
START = int(datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp())
END   = int(datetime(2026, 8, 26, tzinfo=timezone.utc).timestamp())

def get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (i + 1)); continue
            if e.code >= 500:
                time.sleep(1.0 * (i + 1)); continue
            raise
        except Exception:
            time.sleep(1.0 * (i + 1))
    raise RuntimeError("giving up on " + url)

def load_cache(p):
    f = os.path.join(CACHE, p + ".csv")
    if not os.path.exists(f): return {}
    out = {}
    with open(f) as fh:
        for r in csv.reader(fh):
            if not r or r[0] == "ts": continue
            out[int(r[0])] = r
    return out

def save_cache(p, rows):
    f = os.path.join(CACHE, p + ".csv")
    with open(f, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["ts", "low", "high", "open", "close", "volume"])
        for ts in sorted(rows): w.writerow(rows[ts])

os.makedirs(CACHE, exist_ok=True)
for p in PRODUCTS:
    rows = load_cache(p)
    have = set(rows)
    todo = []
    t = START
    while t < END:
        chunk_end = min(t + GRAN * MAXC, END)
        # skip chunk if >=99% already cached
        want = range(t, chunk_end, GRAN)
        miss = sum(1 for x in want if x not in have)
        if miss > len(list(want)) * 0.01:
            todo.append((t, chunk_end))
        t = chunk_end
    print(f"{p}: cached={len(rows)} chunks_to_fetch={len(todo)}", flush=True)
    for i, (a, b) in enumerate(todo):
        u = (f"https://api.exchange.coinbase.com/products/{p}/candles?"
             f"granularity={GRAN}"
             f"&start={datetime.fromtimestamp(a, timezone.utc).isoformat().replace('+00:00','Z')}"
             f"&end={datetime.fromtimestamp(b, timezone.utc).isoformat().replace('+00:00','Z')}")
        for c in get(u):
            rows[int(c[0])] = [int(c[0])] + list(c[1:])
        if i % 50 == 0:
            print(f"  {p} {i}/{len(todo)} rows={len(rows)}", flush=True)
            save_cache(p, rows)
        time.sleep(0.14)
    save_cache(p, rows)
    print(f"{p}: DONE rows={len(rows)}", flush=True)
print("ALL DONE")
