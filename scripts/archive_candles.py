#!/usr/bin/env python3
"""Archive yesterday's settled 15M markets + 1-min candles to data/candles/.

WHY THIS EXISTS
---------------
Kalshi retains settled markets for roughly 67 days. Every strategy question this
project has tried to answer has been blocked by that one fact: by the time a
hypothesis is formed, the only data that exists is the data it was formed on, and
the out-of-sample window is whatever few days sit outside the discovery set.
The NO-side question sat unresolved for months for exactly this reason — the
standing re-entry gate asked for 90 days of clean holdout that the API can never
supply.

Running this nightly turns that around: every day archived today is untouched
out-of-sample data for every hypothesis formed after today. It costs one workflow
run and ~120KB gzipped per day.

Output: data/candles/YYYY-MM-DD.csv.gz, schema-compatible with
backtest_ablation_raw.csv (minus the Coinbase spot columns).

Usage:
    python3 scripts/archive_candles.py               # yesterday (UTC)
    python3 scripts/archive_candles.py 2026-08-15    # a specific date
    python3 scripts/archive_candles.py --backfill 7  # last 7 days, skip existing
"""
import argparse
import csv
import gzip
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M",
               "KXBNB15M", "KXXRP15M", "KXWTI15M"]
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "candles")
FIELDS = ["series", "ticker", "close_ts", "close_hour", "candle_idx", "side", "ask",
          "secs_left", "won", "prior_1", "prior_2", "prior_3", "floor_strike"]

# Match the collection bounds of backtest_ablation_raw.csv so archived days can be
# concatenated with it directly. Deliberately wider than the live entry gates.
ASK_MIN, ASK_MAX = 88, 96
SECS_MIN, SECS_MAX = 100, 800


def parse_close_ts(m):
    ct = m.get("close_time", "")
    if not ct:
        return 0
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def candle_price(c, side):
    """Ask in cents for the given side. NO ask = 100 - YES bid."""
    try:
        if side == "yes":
            return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
        yes_bid = int(round(float(c["yes_bid"]["close_dollars"]) * 100))
        return 100 - yes_bid if yes_bid > 0 else None
    except (KeyError, ValueError, TypeError):
        return None


def fetch_markets(series, day_start, day_end):
    """Settled markets closing inside [day_start, day_end)."""
    keep, cursor = [], None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        try:
            code, r = kalshi_get("/markets", p)
        except Exception:
            time.sleep(2)
            continue
        if code != 200:
            break
        batch = r.get("markets", [])
        if not batch:
            break
        stop = False
        for m in batch:
            cts = parse_close_ts(m)
            if not cts:
                continue
            if cts < day_start:
                stop = True
                break
            if cts < day_end:
                keep.append(m)
        cursor = r.get("cursor")
        if stop or not cursor:
            break
        time.sleep(0.05)
    return keep


def fetch_candles(series, ticker, close_ts):
    for a in range(6):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 900, "end_ts": close_ts + 10,
                 "period_interval": 1})
            if code == 200:
                return sorted(r.get("candlesticks", []),
                              key=lambda c: c.get("end_period_ts", 0))
            if code == 429:
                time.sleep(1.5 * (a + 1))
                continue
        except Exception:
            pass
        time.sleep(min(2 ** a, 8))
    return None


def rows_for_market(m, series):
    ticker, result = m.get("ticker", ""), m.get("result", "")
    close_ts = parse_close_ts(m)
    if not result or not close_ts:
        return [], False
    candles = fetch_candles(series, ticker, close_ts)
    if candles is None:
        return [], True                      # signal a fetch failure
    strike = float(m.get("floor_strike") or 0)
    hour = datetime.fromtimestamp(close_ts, tz=timezone.utc).hour
    out = []
    for i, c in enumerate(candles):
        secs_left = close_ts - c.get("end_period_ts", 0)
        if not (SECS_MIN <= secs_left <= SECS_MAX):
            continue
        for side in ("yes", "no"):
            ask = candle_price(c, side)
            if ask is None or not (ASK_MIN <= ask <= ASK_MAX):
                continue
            pc = lambda o: candle_price(candles[i - o], side) if i - o >= 0 else None
            out.append({
                "series": series, "ticker": ticker, "close_ts": close_ts,
                "close_hour": hour, "candle_idx": i, "side": side, "ask": ask,
                "secs_left": int(secs_left), "won": result == side,
                "prior_1": pc(1) if pc(1) is not None else "",
                "prior_2": pc(2) if pc(2) is not None else "",
                "prior_3": pc(3) if pc(3) is not None else "",
                "floor_strike": strike,
            })
    return out, False


def archive_day(date_str, force=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{date_str}.csv.gz")
    if os.path.exists(path) and not force:
        print(f"{date_str}: already archived, skipping")
        return True

    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start, end = int(day.timestamp()), int((day + timedelta(days=1)).timestamp())
    if end > time.time():
        print(f"{date_str}: day not finished yet, refusing to archive a partial day")
        return False

    all_rows, failures, markets_seen = [], 0, 0
    for series in SERIES_LIST:
        ms = fetch_markets(series, start, end)
        markets_seen += len(ms)
        for m in ms:
            rows, failed = rows_for_market(m, series)
            all_rows += rows
            failures += failed
            time.sleep(0.08)
        print(f"  {series}: {len(ms)} markets", flush=True)

    if failures:
        # A partial day is worse than no day — it would look like a real gap later.
        print(f"{date_str}: ABORT — {failures} candle fetches failed; not writing "
              f"a partial archive. Re-run to retry.")
        return False
    if not all_rows:
        print(f"{date_str}: no qualifying rows ({markets_seen} markets scanned)")
        return False

    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    os.replace(tmp, path)                    # atomic: never leave a half file
    print(f"{date_str}: wrote {len(all_rows):,} rows from {markets_seen} markets "
          f"-> {path} ({os.path.getsize(path)/1024:.0f} KB)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD (default: yesterday UTC)")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="archive the last N complete days, skipping existing")
    ap.add_argument("--force", action="store_true", help="overwrite existing archive")
    a = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    if a.backfill:
        ok = True
        for i in range(a.backfill, 0, -1):
            ok &= archive_day(str(today - timedelta(days=i)), a.force)
        return 0 if ok else 1
    day = a.date or str(today - timedelta(days=1))
    return 0 if archive_day(day, a.force) else 1


if __name__ == "__main__":
    sys.exit(main())
