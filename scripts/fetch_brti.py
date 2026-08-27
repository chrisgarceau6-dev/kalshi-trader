#!/usr/bin/env python3
"""Pull TRUE settlement values (CF Benchmarks RTI) for every settled 15M market.

Kalshi settles these on the 60-second average of CF Benchmarks' Real Time Index before
the close, and sets each market's strike to the SAME quantity 15 minutes earlier — so
`strike[t] == expiration_value[t-15min]`, and the settled markets chain into an exact
RTI grid on 15-minute boundaries. That is the settlement basis itself, for free, with
no CF Benchmarks subscription (their API returns "not authorized").

Cache: data/.brti_cache/<SERIES>.csv  (ticker,open_ts,close_ts,strike,expiration_value,result)
"""
import csv, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", ".brti_cache")
API = "https://api.elections.kalshi.com/trade-api/v2/markets"
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
START = datetime(2026, 6, 10, tzinfo=timezone.utc)
END   = datetime(2026, 8, 27, tzinfo=timezone.utc)

def get(url, tries=5):
    for i in range(tries):
        try:
            r = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1"})
            with urllib.request.urlopen(r, timeout=30) as f:
                return json.loads(f.read())
        except urllib.error.HTTPError as e:
            if e.code in (429,) or e.code >= 500:
                time.sleep(1.5 * (i + 1)); continue
            raise
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None

def ts(s):
    try: return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception: return None

os.makedirs(CACHE, exist_ok=True)
for series in SERIES:
    path = os.path.join(CACHE, series + ".csv")
    rows = {}
    if os.path.exists(path):
        with open(path) as fh:
            for r in csv.DictReader(fh): rows[r["ticker"]] = r
    day, n0 = START, len(rows)
    while day < END:
        nxt = day + timedelta(days=1)
        # /markets accepts min_close_ts/max_close_ts — one page per day, ~0.2s (see §6)
        u = (f"{API}?series_ticker={series}&status=settled"
             f"&min_close_ts={int(day.timestamp())}&max_close_ts={int(nxt.timestamp())}&limit=1000")
        d = get(u)
        for m in (d or {}).get("markets", []):
            ev, fs = m.get("expiration_value"), m.get("floor_strike")
            if ev in (None, "") or fs in (None, ""): continue
            rows[m["ticker"]] = dict(ticker=m["ticker"], open_ts=ts(m.get("open_time", "")),
                                     close_ts=ts(m.get("close_time", "")), strike=fs,
                                     expiration_value=ev, result=m.get("result", ""))
        day = nxt
        time.sleep(0.08)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker","open_ts","close_ts","strike","expiration_value","result"])
        w.writeheader()
        for t in sorted(rows, key=lambda k: rows[k]["close_ts"] or 0): w.writerow(rows[t])
    print(f"{series}: {len(rows)} settled markets (+{len(rows)-n0})", flush=True)
print("ALL DONE")
