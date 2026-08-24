#!/usr/bin/env python3
"""Backtest: ultra-late scalp strategy.

Buys YES or NO at 96-99c with <120s remaining.
Thesis: markets at 96-99c with under 2 minutes left are essentially resolved.
Complements the main strategy (90-95c, 150-600s) with no overlap.

Run: python3 backtest_ultralate.py [--days 60]
"""

import csv, time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

ENTRY_MIN  = 96
ENTRY_MAX  = 99
MAX_SECS   = 120
MIN_SECS   = 10   # ignore last 10s (settlement race)
BLACKOUT   = {15, 16, 17}
FEE        = 0.07
BET        = 45

BUCKETS = {
    "96c": (96, 96),
    "97c": (97, 97),
    "98c": (98, 98),
    "99c": (99, 99),
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


def trade_pnl(won, ask_cents):
    contracts = BET / (ask_cents / 100)
    if won:
        return round(contracts * (1.0 - ask_cents / 100) * (1 - FEE), 2)
    return -BET


def breakeven_wr(ask_cents):
    p = ask_cents / 100
    return p / (p + (1 - p) * (1 - FEE)) * 100


def fetch_settled_markets(series, cutoff_ts):
    markets, cursor = [], None
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
                markets.append(m)
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return markets


def fetch_candles(ticker, series, close_ts):
    for attempt in range(3):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 300, "end_ts": close_ts + 10, "period_interval": 1},
            )
            if code == 200:
                return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return None


def simulate(market):
    ticker   = market.get("ticker", "")
    ev       = market.get("event_ticker", "") or ticker
    series   = ev.split("-")[0]
    result   = market.get("result", "")
    close_ts = parse_close_ts(market)
    if not result or not close_ts:
        return []

    close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
    if close_dt.hour in BLACKOUT:
        return []

    candles = fetch_candles(ticker, series, close_ts)
    if not candles:
        return []

    outcomes = []
    fired = set()

    for c in candles:
        end_ts    = c.get("end_period_ts", 0)
        secs_left = close_ts - end_ts
        if not (MIN_SECS <= secs_left <= MAX_SECS):
            continue

        for side in ("yes", "no"):
            if side in fired:
                continue
            ask = candle_ask_cents(c, side)
            if ask is None or not (ENTRY_MIN <= ask <= ENTRY_MAX):
                continue

            bucket = f"{ask}c"
            won    = result == side
            outcomes.append({
                "series":    series,
                "ticker":    ticker,
                "side":      side,
                "ask":       ask,
                "bucket":    bucket,
                "secs_left": int(secs_left),
                "won":       won,
                "profit":    trade_pnl(won, ask),
            })
            fired.add(side)

    return outcomes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out",  default="backtest_ultralate.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Ultra-late scalp backtest — {args.days}d, {len(SERIES_LIST)} series")
    print(f"Entry: [{ENTRY_MIN},{ENTRY_MAX}]c, <{MAX_SECS}s remaining, ${BET} bets")
    print(f"\nBreak-even WR by price:")
    for ask in range(96, 100):
        print(f"  {ask}c: BE={breakeven_wr(ask):.1f}%")
    print()

    rows = []
    for series in SERIES_LIST:
        print(f"[{series}] fetching...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        print(f"[{series}] {len(markets)} markets", flush=True)
        for idx, m in enumerate(markets):
            if idx % 100 == 0:
                print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)
            rows.extend(simulate(m))
        print(f"[{series}] done          ")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series","ticker","side","ask","bucket","secs_left","won","profit"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'Price':<8} {'n':>6}  {'WR':>7}  {'BE-WR':>7}  {'Net P&L':>10}  {'$/trade':>8}  {'edge':>7}")
    print("-" * 65)
    total_n = total_wins = 0; total_pnl = 0.0
    for ask in range(96, 100):
        bucket = f"{ask}c"
        subset = [r for r in rows if r["bucket"] == bucket]
        if not subset:
            print(f"{bucket:<8} {'—':>6}")
            continue
        n    = len(subset)
        wins = sum(1 for r in subset if r["won"])
        pnl  = sum(r["profit"] for r in subset)
        wr   = wins / n * 100
        be   = breakeven_wr(ask)
        print(f"{bucket:<8} {n:>6}  {wr:>6.1f}%  {be:>6.1f}%  ${pnl:>+10.0f}  ${pnl/n:>+7.2f}/trade  {wr-be:>+6.1f}pp")
        total_n += n; total_wins += wins; total_pnl += pnl
    print("-" * 65)
    if total_n:
        print(f"{'TOTAL':<8} {total_n:>6}  {total_wins/total_n*100:>6.1f}%             ${total_pnl:>+10.0f}  ${total_pnl/total_n:>+7.2f}/trade")
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
