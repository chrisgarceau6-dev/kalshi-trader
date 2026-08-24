#!/usr/bin/env python3
"""Backtest: candle acceleration filter.

Among current-strategy trades (ask 90-95c, prior 3 candles >= 80c, 150-600s left),
compare WR/P&L for:
  - ACCELERATING: each prior candle >= previous (80 → 87 → 93)
  - FLAT/DECELERATING: prior candles pass 80c gate but not consistently increasing

Run: python3 backtest_acceleration.py [--days 60]
"""

import csv, time, argparse
from datetime import datetime, timezone
from collections import defaultdict
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

MIN_ASK     = 90
MAX_ASK     = 95
PRIOR_K     = 3
PRIOR_MIN   = 80
MIN_SECS    = 150
MAX_SECS    = 600
BLACKOUT    = {15, 16, 17}
FEE         = 0.07
BET         = 45


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


def is_accelerating(prior_asks):
    """Each candle >= the one before it."""
    return all(prior_asks[i] >= prior_asks[i-1] for i in range(1, len(prior_asks)))


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
                {"start_ts": close_ts - 1200, "end_ts": close_ts + 30, "period_interval": 1},
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

    for i, c in enumerate(candles):
        end_ts    = c.get("end_period_ts", 0)
        secs_left = close_ts - end_ts
        if not (MIN_SECS <= secs_left <= MAX_SECS):
            continue

        for side in ("yes", "no"):
            if side in fired:
                continue
            ask = candle_ask_cents(c, side)
            if ask is None or not (MIN_ASK <= ask <= MAX_ASK):
                continue

            prior = candles[max(0, i - PRIOR_K):i]
            if len(prior) < PRIOR_K:
                continue
            prior_asks = [candle_ask_cents(pc, side) for pc in prior[-PRIOR_K:]]
            if any(pa is None for pa in prior_asks):
                continue
            if any(pa < PRIOR_MIN for pa in prior_asks):
                continue

            accel = is_accelerating(prior_asks)
            won   = result == side
            outcomes.append({
                "series":    series,
                "ticker":    ticker,
                "side":      side,
                "ask":       ask,
                "secs_left": int(secs_left),
                "prior_asks": ",".join(str(x) for x in prior_asks),
                "accel":     accel,
                "won":       won,
                "profit":    trade_pnl(won, ask),
            })
            fired.add(side)

    return outcomes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out",  default="backtest_acceleration.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Acceleration filter backtest — {args.days}d, {len(SERIES_LIST)} series")
    print(f"Base strategy: ask [{MIN_ASK},{MAX_ASK}]c, prior {PRIOR_K} candles >= {PRIOR_MIN}c, {MIN_SECS}-{MAX_SECS}s left")
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
        w = csv.DictWriter(f, fieldnames=["series","ticker","side","ask","secs_left","prior_asks","accel","won","profit"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'Group':<22} {'n':>6}  {'WR':>7}  {'Net P&L':>10}  {'$/trade':>8}")
    print("-" * 60)
    for accel in [True, False]:
        label = "ACCELERATING" if accel else "FLAT/DECEL"
        subset = [r for r in rows if r["accel"] == accel]
        if not subset:
            print(f"{label:<22} {'—':>6}")
            continue
        n    = len(subset)
        wins = sum(1 for r in subset if r["won"])
        pnl  = sum(r["profit"] for r in subset)
        print(f"{label:<22} {n:>6}  {wins/n*100:>6.1f}%  ${pnl:>+10.0f}  ${pnl/n:>+7.2f}/trade")
    print("-" * 60)
    n    = len(rows)
    wins = sum(1 for r in rows if r["won"])
    pnl  = sum(r["profit"] for r in rows)
    if n:
        print(f"{'ALL':<22} {n:>6}  {wins/n*100:>6.1f}%  ${pnl:>+10.0f}  ${pnl/n:>+7.2f}/trade")
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
