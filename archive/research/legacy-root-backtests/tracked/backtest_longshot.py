#!/usr/bin/env python3
"""Backtest: crash-reversal longshot strategy.

Buys YES or NO at 5-25c when the prior 3 candles on the same side
averaged >= PRIOR_AVG_THRESH cents. Thesis: market overreacts to a
sudden move, and snapback happens more than the crashed price implies.

Run:
    python backtest_longshot.py              # 60 days, all 8 series
    python backtest_longshot.py --days 30
    python backtest_longshot.py --days 74    # full history
"""

import csv, json, os, sys, time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M",
    "KXBNB15M", "KXXRP15M", "KXHYPE15M", "KXNEAR15M",
]

# ── Strategy parameters ───────────────────────────────────────────────────────
# Entry: current ask in [5, 25]c (longshot zone)
MAX_ENTRY_CENTS  = 25
MIN_ENTRY_CENTS  = 5

# Prior signal: avg of last 3 candles (same side) must be >= this to confirm crash
PRIOR_K          = 3
PRIOR_AVG_THRESH = 60   # prior avg >= 60c means market was high-certainty before crash

# Time window: wider than current strategy to give reversal time to develop
MIN_SECS_LEFT    = 300
MAX_SECS_LEFT    = 900

BLACKOUT_HOURS   = {15, 16, 17}
FEE              = 0.07
BET_SIZE         = 35

# Per-cent entry buckets to analyse
BUCKETS = {
    " 5-9c":  (5,  9),
    "10-14c": (10, 14),
    "15-19c": (15, 19),
    "20-25c": (20, 25),
}


def parse_close_ts(market):
    ct = market.get("close_time", "")
    if not ct:
        return 0
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def candle_ask_cents(c, side):
    try:
        if side == "yes":
            return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
        else:
            yes_bid = int(round(float(c["yes_bid"]["close_dollars"]) * 100))
            return 100 - yes_bid if yes_bid > 0 else 100
    except (KeyError, ValueError, TypeError):
        return None


def pnl(won, ask_cents):
    contracts = BET_SIZE / (ask_cents / 100)
    if won:
        return round(contracts * (1.0 - ask_cents / 100) * (1 - FEE), 2)
    return -BET_SIZE


def breakeven_wr(ask_cents):
    p = ask_cents / 100
    return round(p / (p + (1 - p) * (1 - FEE)) * 100, 1)


def fetch_settled_markets(series, cutoff_ts, max_ts=None):
    markets = []
    cursor = None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            code, r = kalshi_get("/markets", params)
        except Exception:
            time.sleep(2)
            continue
        if code != 200:
            break
        batch = r.get("markets", [])
        if not batch:
            break
        stopped = False
        for m in batch:
            close_ts = parse_close_ts(m)
            if close_ts and close_ts < cutoff_ts:
                stopped = True
                break
            if close_ts:
                if max_ts is None or close_ts <= max_ts:
                    markets.append(m)
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return markets


def fetch_candles(ticker, series, close_ts, retries=3):
    for attempt in range(retries):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 1200, "end_ts": close_ts + 30, "period_interval": 1},
            )
            if code == 200:
                return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
            return None
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def simulate(market):
    """Return list of {bucket, won, ask, profit, side, secs_left, prior_avg}."""
    ticker   = market.get("ticker", "")
    ev       = market.get("event_ticker", "") or ticker
    series   = ev.split("-")[0]
    result   = market.get("result", "")
    close_ts = parse_close_ts(market)

    if not result or not close_ts:
        return []

    close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
    if close_dt.hour in BLACKOUT_HOURS:
        return []

    candles = fetch_candles(ticker, series, close_ts)
    if not candles:
        return []

    outcomes = []
    fired_buckets = set()

    for i, c in enumerate(candles):
        end_ts    = c.get("end_period_ts", 0)
        secs_left = close_ts - end_ts
        if not (MIN_SECS_LEFT <= secs_left <= MAX_SECS_LEFT):
            continue

        for side in ("yes", "no"):
            ask = candle_ask_cents(c, side)
            if ask is None or not (MIN_ENTRY_CENTS <= ask <= MAX_ENTRY_CENTS):
                continue

            # Prior K candles on same side must average >= PRIOR_AVG_THRESH
            prior = candles[max(0, i - PRIOR_K):i]
            if len(prior) < PRIOR_K:
                continue
            prior_asks = [candle_ask_cents(pc, side) for pc in prior[-PRIOR_K:]]
            if any(pa is None for pa in prior_asks):
                continue
            prior_avg = sum(prior_asks) / len(prior_asks)
            if prior_avg < PRIOR_AVG_THRESH:
                continue

            # Find bucket
            bucket = None
            for bname, (bmin, bmax) in BUCKETS.items():
                if bmin <= ask <= bmax:
                    bucket = bname
                    break
            if bucket is None or bucket in fired_buckets:
                continue

            won = result == side
            outcomes.append({
                "bucket":    bucket,
                "won":       won,
                "ask":       ask,
                "profit":    pnl(won, ask),
                "side":      side,
                "secs_left": int(secs_left),
                "prior_avg": round(prior_avg, 1),
                "ticker":    ticker,
                "series":    series,
            })
            fired_buckets.add(bucket)

    return outcomes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",     type=int,  default=60)
    ap.add_argument("--oos-days", type=int,  default=None,
                    help="exclude markets newer than this many days ago (OOS window)")
    ap.add_argument("--series", nargs="+",   default=SERIES_LIST)
    ap.add_argument("--out",    default="backtest_longshot.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    max_ts = int(time.time()) - args.oos_days * 86400 if args.oos_days else None

    window_label = f"days {args.oos_days}-{args.days} (OOS)" if args.oos_days else f"{args.days}d"
    print(f"Crash-reversal longshot backtest — {window_label}, {len(args.series)} series")
    print(f"Signal: ask [{MIN_ENTRY_CENTS},{MAX_ENTRY_CENTS}]c, prior {PRIOR_K}-candle avg >= {PRIOR_AVG_THRESH}c")
    print(f"Window: {MIN_SECS_LEFT}-{MAX_SECS_LEFT}s remaining, ${BET_SIZE} bets")
    print(f"Break-even WR by entry bucket:")
    for bname, (bmin, bmax) in BUCKETS.items():
        mid = (bmin + bmax) // 2
        print(f"  {bname}: BE={breakeven_wr(mid)}%")
    print()

    stats   = {b: {"n": 0, "wins": 0, "profit": 0.0} for b in BUCKETS}
    rows    = []

    for series in args.series:
        print(f"[{series}] fetching markets...", flush=True)
        markets = fetch_settled_markets(series, cutoff, max_ts)
        print(f"[{series}] {len(markets)} markets — simulating...", flush=True)

        for idx, m in enumerate(markets):
            if idx % 100 == 0:
                print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)
            for o in simulate(m):
                b = o["bucket"]
                stats[b]["n"]      += 1
                stats[b]["wins"]   += int(o["won"])
                stats[b]["profit"] += o["profit"]
                rows.append(o)

        print(f"[{series}] done ({len(markets)} markets)          ")

    # ── Results ───────────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print(f"CRASH-REVERSAL LONGSHOT  ({args.days}d, {len(args.series)} series, ${BET_SIZE} bets)")
    print(f"Prior {PRIOR_K}-candle avg >= {PRIOR_AVG_THRESH}c  |  entry [{MIN_ENTRY_CENTS},{MAX_ENTRY_CENTS}]c  |  {MIN_SECS_LEFT}-{MAX_SECS_LEFT}s left")
    print("=" * 65)
    print(f"{'Bucket':<10} {'n':>6}  {'WR':>7}  {'BE-WR':>7}  {'Net P&L':>10}")
    print("-" * 65)
    total_n = total_wins = 0
    total_pnl = 0.0
    for bname, (bmin, bmax) in BUCKETS.items():
        s = stats[bname]
        n = s["n"]
        if n == 0:
            print(f"{bname:<10} {'—':>6}")
            continue
        wr  = s["wins"] / n * 100
        be  = breakeven_wr((bmin + bmax) // 2)
        pnl_total = s["profit"]
        print(f"{bname:<10} {n:>6}  {wr:>6.1f}%  {be:>6.1f}%  ${pnl_total:>+10.0f}  (edge {wr-be:+.1f}pp)")
        total_n    += n
        total_wins += s["wins"]
        total_pnl  += pnl_total
    print("=" * 65)
    if total_n:
        print(f"{'TOTAL':<10} {total_n:>6}  {total_wins/total_n*100:>6.1f}%             ${total_pnl:>+10.0f}")
    print()

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series","ticker","bucket","side","ask","prior_avg","secs_left","won","profit"])
        w.writeheader()
        w.writerows(rows)
    print(f"Raw trades saved to {args.out}")


if __name__ == "__main__":
    main()
