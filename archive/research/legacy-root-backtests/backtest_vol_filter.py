#!/usr/bin/env python3
"""Backtest: price-volatility filter on late-certainty strategy.

For each eligible entry (90-95c, 150-600s, 3 prior candles ≥80c),
computes std dev of the side's price over the prior VOL_WINDOW candles.
Shows WR/P&L split by vol bucket to test: low vol → higher WR?

Run: python3 backtest_vol_filter.py [--days 60]
"""

import csv, time, argparse, statistics
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

ENTRY_MIN  = 90
ENTRY_MAX  = 95
MIN_SECS   = 150
MAX_SECS   = 600
PRIOR_MIN  = 80
PRIOR_N    = 3
VOL_WINDOW = 5
BLACKOUT   = {15, 16, 17}
FEE        = 0.07
BET        = 45

VOL_BUCKETS = [("low", 0, 3), ("med", 3, 7), ("high", 7, 999)]


def parse_close_ts(market):
    ct = market.get("close_time", "")
    if not ct:
        return 0
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def candle_price(c, side):
    try:
        if side == "yes":
            return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
        else:
            yes_bid = int(round(float(c["yes_bid"]["close_dollars"]) * 100))
            return 100 - yes_bid if yes_bid > 0 else None
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


def vol_bucket_name(sigma):
    for name, lo, hi in VOL_BUCKETS:
        if lo <= sigma < hi:
            return name
    return "high"


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
                {"start_ts": close_ts - 900, "end_ts": close_ts + 10, "period_interval": 1},
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
            ask = candle_price(c, side)
            if ask is None or not (ENTRY_MIN <= ask <= ENTRY_MAX):
                continue

            prior = candles[max(0, i - PRIOR_N):i]
            if len(prior) < PRIOR_N:
                continue
            prior_prices = [candle_price(p, side) for p in prior]
            if any(p is None or p < PRIOR_MIN for p in prior_prices):
                continue

            vol_candles = candles[max(0, i - VOL_WINDOW):i]
            vol_prices  = [candle_price(p, side) for p in vol_candles]
            vol_prices  = [p for p in vol_prices if p is not None]
            sigma = statistics.stdev(vol_prices) if len(vol_prices) >= 2 else 0.0

            won = result == side
            outcomes.append({
                "series":     series,
                "ticker":     ticker,
                "side":       side,
                "ask":        ask,
                "secs_left":  int(secs_left),
                "sigma":      round(sigma, 2),
                "vol_bucket": vol_bucket_name(sigma),
                "won":        won,
                "profit":     trade_pnl(won, ask),
            })
            fired.add(side)

    return outcomes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out",  default="backtest_vol_filter.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Vol-filter backtest — {args.days}d, {len(SERIES_LIST)} series")
    print(f"Entry: [{ENTRY_MIN},{ENTRY_MAX}]c, {MIN_SECS}-{MAX_SECS}s, {PRIOR_N} prior ≥{PRIOR_MIN}c")
    print(f"Vol window: {VOL_WINDOW} candles, buckets: low<3c, med 3-7c, high≥7c\n")

    rows = []
    series_times = []
    for series in SERIES_LIST:
        t0 = time.time()
        print(f"[{series}] fetching...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        print(f"[{series}] {len(markets)} markets", flush=True)
        for idx, m in enumerate(markets):
            if idx % 100 == 0:
                elapsed = time.time() - t0
                remaining_series = len(SERIES_LIST) - len(series_times) - 1
                if idx > 0 and series_times:
                    avg_series_min = sum(series_times) / len(series_times)
                    eta_min = (len(markets) - idx) / len(markets) * (elapsed / 60) + remaining_series * avg_series_min
                    print(f"  {idx}/{len(markets)}  ETA ~{eta_min:.0f}min remaining", end="\r", flush=True)
                else:
                    print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)
            rows.extend(simulate(m))
        elapsed = time.time() - t0
        series_times.append(elapsed / 60)
        remaining = len(SERIES_LIST) - len(series_times)
        avg_min   = sum(series_times) / len(series_times)
        print(f"[{series}] done — {elapsed/60:.1f}min (avg {avg_min:.1f}min/series, ~{remaining * avg_min:.0f}min left)          ")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series","ticker","side","ask","secs_left","sigma","vol_bucket","won","profit"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'Vol':<8} {'n':>6}  {'WR':>7}  {'Net P&L':>10}  {'$/trade':>8}")
    print("-" * 52)
    total_n = total_wins = 0; total_pnl = 0.0
    for name, lo, hi in VOL_BUCKETS:
        subset = [r for r in rows if r["vol_bucket"] == name]
        if not subset:
            print(f"{name:<8} {'—':>6}")
            continue
        n    = len(subset)
        wins = sum(1 for r in subset if r["won"])
        pnl  = sum(r["profit"] for r in subset)
        wr   = wins / n * 100
        print(f"{name:<8} {n:>6}  {wr:>6.1f}%  ${pnl:>+10.0f}  ${pnl/n:>+7.2f}/trade")
        total_n += n; total_wins += wins; total_pnl += pnl
    print("-" * 52)
    if total_n:
        print(f"{'TOTAL':<8} {total_n:>6}  {total_wins/total_n*100:>6.1f}%  ${total_pnl:>+10.0f}  ${total_pnl/total_n:>+7.2f}/trade")

    print(f"\n{'Ask':<6} {'n':>6}  {'WR':>7}  {'Net P&L':>10}  {'$/trade':>8}  {'BE':>6}")
    print("-" * 52)
    for ask in range(90, 96):
        subset = [r for r in rows if r["ask"] == ask]
        if not subset:
            continue
        n    = len(subset)
        wins = sum(1 for r in subset if r["won"])
        pnl  = sum(r["profit"] for r in subset)
        wr   = wins / n * 100
        be   = breakeven_wr(ask)
        print(f"{ask}c     {n:>6}  {wr:>6.1f}%  ${pnl:>+10.0f}  ${pnl/n:>+7.2f}/trade  {be:.1f}%")

    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
