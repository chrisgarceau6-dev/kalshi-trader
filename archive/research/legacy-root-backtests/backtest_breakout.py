#!/usr/bin/env python3
"""Backtest: stuck-market breakout strategy.

Buys YES or NO at 65-85c when the market has been stuck near 50c for
at least STUCK_K prior candles, then breaks sharply upward.

Thesis: a market that was genuinely uncertain (50c) then breaks directionally
has momentum behind it — the market is starting to price in the outcome.

Run: python3 backtest_breakout.py [--days 60]
"""

import csv, time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

# Entry zone: market just broke out of stuck zone
ENTRY_MIN   = 65
ENTRY_MAX   = 85

# "Stuck" definition: prior candles all within this band
STUCK_LO    = 35
STUCK_HI    = 65
STUCK_K     = 4   # number of prior candles that must be in stuck zone

# Time remaining
MIN_SECS    = 300
MAX_SECS    = 900

BLACKOUT    = {15, 16, 17}
FEE         = 0.07
BET         = 45

BUCKETS = {
    "65-69c": (65, 69),
    "70-74c": (70, 74),
    "75-79c": (75, 79),
    "80-85c": (80, 85),
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
                {"start_ts": close_ts - 1500, "end_ts": close_ts + 30, "period_interval": 1},
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
            if ask is None or not (ENTRY_MIN <= ask <= ENTRY_MAX):
                continue

            # Prior STUCK_K candles must all be in stuck zone
            prior = candles[max(0, i - STUCK_K):i]
            if len(prior) < STUCK_K:
                continue
            prior_asks = [candle_ask_cents(pc, side) for pc in prior[-STUCK_K:]]
            if any(pa is None for pa in prior_asks):
                continue
            if not all(STUCK_LO <= pa <= STUCK_HI for pa in prior_asks):
                continue

            bucket = next((b for b, (lo, hi) in BUCKETS.items() if lo <= ask <= hi), None)
            if not bucket:
                continue

            won = result == side
            outcomes.append({
                "series":      series,
                "ticker":      ticker,
                "side":        side,
                "ask":         ask,
                "bucket":      bucket,
                "secs_left":   int(secs_left),
                "prior_stuck": ",".join(str(x) for x in prior_asks),
                "won":         won,
                "profit":      trade_pnl(won, ask),
            })
            fired.add(side)

    return outcomes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out",  default="backtest_breakout.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Stuck-market breakout backtest — {args.days}d, {len(SERIES_LIST)} series")
    print(f"Entry: [{ENTRY_MIN},{ENTRY_MAX}]c after {STUCK_K}+ candles in [{STUCK_LO},{STUCK_HI}]c")
    print(f"Window: {MIN_SECS}-{MAX_SECS}s remaining, ${BET} bets")
    print(f"\nBreak-even WR by bucket:")
    for b, (lo, hi) in BUCKETS.items():
        mid = (lo + hi) // 2
        print(f"  {b}: BE={breakeven_wr(mid):.1f}%")
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
        w = csv.DictWriter(f, fieldnames=["series","ticker","side","ask","bucket","secs_left","prior_stuck","won","profit"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'Bucket':<10} {'n':>6}  {'WR':>7}  {'BE-WR':>7}  {'Net P&L':>10}  {'$/trade':>8}  {'edge':>7}")
    print("-" * 68)
    total_n = total_wins = 0
    total_pnl = 0.0
    for b, (lo, hi) in BUCKETS.items():
        subset = [r for r in rows if r["bucket"] == b]
        if not subset:
            print(f"{b:<10} {'—':>6}")
            continue
        n    = len(subset)
        wins = sum(1 for r in subset if r["won"])
        pnl  = sum(r["profit"] for r in subset)
        wr   = wins / n * 100
        be   = breakeven_wr((lo + hi) // 2)
        print(f"{b:<10} {n:>6}  {wr:>6.1f}%  {be:>6.1f}%  ${pnl:>+10.0f}  ${pnl/n:>+7.2f}/trade  {wr-be:>+6.1f}pp")
        total_n += n; total_wins += wins; total_pnl += pnl
    print("-" * 68)
    if total_n:
        print(f"{'TOTAL':<10} {total_n:>6}  {total_wins/total_n*100:>6.1f}%             ${total_pnl:>+10.0f}  ${total_pnl/total_n:>+7.2f}/trade")
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
