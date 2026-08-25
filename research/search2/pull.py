#!/usr/bin/env python3
"""Step 3: bulk-pull a GENERAL market archive for the strategy-2 search.

WHY A NEW ARCHIVE
-----------------
data/candles/ is purpose-built for the late-certainty strategy: only ask 88-96c, only
150-900s to close, only 9 crypto/commodity series. You cannot find a NEW strategy in
data shaped around the old one — the band filter alone discards ~everything a
different edge would live in.

This pulls the FULL price path of every settled market in scope: both sides, whole
lifetime, no band filter, no time-window filter. It is deliberately fat, because the
scan in step 4 does not yet know what it is looking for.

Kalshi retains ~67 days, so this reads BACKWARD. No waiting to collect forward.

SCOPE comes from universe.json (step 2), ranked by volume — volume decides whether an
edge is executable, and observations already clear for everything in that file.

    python3 research/search2/pull.py --list           # what would be pulled
    python3 research/search2/pull.py --top 8          # pull the 8 most liquid
    python3 research/search2/pull.py --series KXHIGHLAX KXWTI

Writes research/search2/data/<SERIES>.csv.gz, one row per (market, minute, side):
    series,ticker,close_ts,ts,secs_left,side,bid,ask,volume,open_interest,won,strike

RESUMABLE: a series already on disk is skipped unless --refresh. The pull is slow and
rate-limited; losing it to a timeout should cost minutes, not hours.
"""
import argparse
import csv
import gzip
import json
import os
import sys
import time
import datetime as D
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1]
sys.path.insert(0, str(BASE))
DATA = HERE / "data"
UNIVERSE = HERE / "universe.json"

FIELDS = ["series", "ticker", "close_ts", "ts", "secs_left", "side",
          "bid", "ask", "volume", "open_interest", "won", "strike"]


def _dotenv():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def close_ts(m):
    try:
        return int(D.datetime.fromisoformat(
            m["close_time"].replace("Z", "+00:00")).timestamp())
    except (KeyError, ValueError, TypeError):
        return None


def settled_markets(api, series, max_pages=40):
    """Every settled market for a series, walking the cursor."""
    out, cur, pages = [], None, 0
    while pages < max_pages:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cur:
            p["cursor"] = cur
        code, r = api.get("/markets", p)
        if code != 200:
            break
        mk = r.get("markets", [])
        if not mk:
            break
        out += mk
        pages += 1
        cur = r.get("cursor")
        if not cur:
            break
        time.sleep(0.08)
    return out


def market_rows(api, series, m, lookback):
    """Full 1-minute price path for one market, both sides, no band filter."""
    cts = close_ts(m)
    result = m.get("result")
    if cts is None or result not in ("yes", "no"):
        return []
    code, r = api.get(f"/series/{series}/markets/{m['ticker']}/candlesticks",
                      {"start_ts": cts - lookback, "end_ts": cts,
                       "period_interval": 1})
    if code != 200:
        return []
    strike = m.get("floor_strike")
    rows = []
    for c in r.get("candlesticks", []):
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        try:
            ybid = float(c["yes_bid"]["close_dollars"]) * 100
            yask = float(c["yes_ask"]["close_dollars"]) * 100
        except (KeyError, TypeError, ValueError):
            continue
        vol = c.get("volume") or c.get("volume_fp") or 0
        oi = c.get("open_interest") or c.get("open_interest_fp") or 0
        # Store BOTH sides explicitly. A NO quote is the mirror of the YES book, and
        # a scan that only sees YES silently halves the search space — the same defect
        # that made every pre-2026-08-22 fill-quality figure a YES-only statistic.
        rows.append([series, m["ticker"], cts, ts, cts - ts, "yes",
                     f"{ybid:.4f}", f"{yask:.4f}", vol, oi,
                     result == "yes", strike])
        rows.append([series, m["ticker"], cts, ts, cts - ts, "no",
                     f"{100 - yask:.4f}", f"{100 - ybid:.4f}", vol, oi,
                     result == "no", strike])
    return rows


def pull_series(api, series, lookback, max_markets):
    mk = settled_markets(api, series)
    mk = [m for m in mk if m.get("result") in ("yes", "no")][:max_markets]
    rows = []
    for i, m in enumerate(mk, 1):
        rows += market_rows(api, series, m, lookback)
        if i % 100 == 0:
            print(f"      {i}/{len(mk)} markets, {len(rows):,} rows", flush=True)
        time.sleep(0.03)
    return mk, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--series", nargs="+")
    ap.add_argument("--lookback", type=int, default=7200,
                    help="seconds of price path before close (default 2h)")
    ap.add_argument("--max-markets", type=int, default=600,
                    help="cap markets per series so one dense ladder cannot eat the run")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    uni = json.loads(UNIVERSE.read_text())
    live = {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"}
    pool = [u for u in uni if u["volume"] > 0 and u["ticker"] not in live]
    pool.sort(key=lambda u: -u["volume"])
    targets = a.series or [u["ticker"] for u in pool[:a.top]]

    if a.list:
        by = {u["ticker"]: u for u in uni}
        print(f"{'series':<20}{'freq':<12}{'strikes':>8}{'volume':>13}  category")
        for t in targets:
            u = by.get(t, {})
            print(f"{t:<20}{u.get('freq',''):<12}{u.get('strikes_per_event',0):>8.0f}"
                  f"{u.get('volume',0):>13,}  {u.get('category','')[:26]}")
        return 0

    DATA.mkdir(parents=True, exist_ok=True)
    _dotenv()
    import kalshi_auth as K

    t0 = time.time()
    for n, s in enumerate(targets, 1):
        out = DATA / f"{s}.csv.gz"
        if out.exists() and not a.refresh:
            print(f"[{n}/{len(targets)}] {s}: cached, skipping")
            continue
        print(f"[{n}/{len(targets)}] {s}: pulling...", flush=True)
        try:
            mk, rows = pull_series(K, s, a.lookback, a.max_markets)
        except Exception as exc:
            print(f"    FAILED {s}: {type(exc).__name__}: {exc}")
            continue
        if not rows:
            print(f"    {s}: no rows (no candlestick history?)")
            continue
        with gzip.open(out, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            w.writerows(rows)
        print(f"    {s}: {len(mk):,} markets -> {len(rows):,} rows, "
              f"{out.stat().st_size/1024:.0f}KB  [{time.time()-t0:.0f}s elapsed]")
    print(f"\ndone in {time.time()-t0:.0f}s -> {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
