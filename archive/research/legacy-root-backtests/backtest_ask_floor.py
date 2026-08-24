#!/usr/bin/env python3
"""Backtest: test lowering the ask floor below 90c.

Pulls settled markets from Kalshi for the last N days, replays
candlestick data through the late-certainty filter, and reports
WR + P&L for each ask zone independently.

Run:
    python backtest_ask_floor.py              # 14 days, all 8 series
    python backtest_ask_floor.py --days 60    # full 60-day window (slow)
    python backtest_ask_floor.py --days 7 --series KXBTC15M KXSOL15M

Needs: KALSHI_API_KEY_ID env var + kalshi_key.pem in cwd.
"""

import csv, json, os, sys, time, argparse
from datetime import datetime, timezone
from collections import defaultdict
from kalshi_auth import get as kalshi_get

# ── Strategy constants (mirror live trader) ─────────────────────────────────
SERIES_LIST = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M",
    "KXBNB15M", "KXXRP15M", "KXHYPE15M", "KXNEAR15M",
]
PRIOR_MIN_CENTS = 80
PRIOR_K         = 3
MIN_SECS_LEFT   = 150
MAX_SECS_LEFT   = 600
BLACKOUT_HOURS  = {15, 16, 17}  # UTC
FEE             = 0.07
BET_SIZE        = 35  # dollars — current live bet at ~$710 balance

# ── Zones to test ────────────────────────────────────────────────────────────
# Pass --buckets to split 90-95c into individual cent buckets.
# Default: broad zones for ask-floor exploration.
ZONES = {
    "75-79c": (75, 79),
    "80-84c": (80, 84),
    "85-89c": (85, 89),
    "90-95c": (90, 95),
}
ZONES_BUCKET = {f"{c}c": (c, c) for c in range(90, 96)}


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_settled_markets(series, cutoff_ts):
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
            print(f"  [{series}] ERROR {code} fetching markets", flush=True)
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


def fetch_candles(ticker, series, close_ts, retries=3):
    for attempt in range(retries):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 900, "end_ts": close_ts + 30, "period_interval": 1},
            )
            if code == 200:
                return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
            return None
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
    return None


# ── Per-market simulation ─────────────────────────────────────────────────────

def simulate(market, prior_filter=True, zones=None):
    """Return dict of zone_name -> {won, ask, profit, side, secs_left} or None."""
    if zones is None:
        zones = ZONES
    ticker   = market.get("ticker", "")
    ev       = market.get("event_ticker", "") or ticker
    series   = ev.split("-")[0]
    result   = market.get("result", "")
    close_ts = parse_close_ts(market)

    if not result or not close_ts:
        return {}

    close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
    if close_dt.hour in BLACKOUT_HOURS:
        return {}

    candles = fetch_candles(ticker, series, close_ts)
    if not candles:
        return {}

    zone_outcomes = {}
    # Track which zones have already fired so we take the first trigger per zone.
    fired = set()

    for i, c in enumerate(candles):
        end_ts    = c.get("end_period_ts", 0)
        secs_left = close_ts - end_ts
        if not (MIN_SECS_LEFT <= secs_left <= MAX_SECS_LEFT):
            continue

        # YES first, then NO — mirrors live trader
        for side in ("yes", "no"):
            ask = candle_ask_cents(c, side)
            if ask is None:
                continue

            for zone_name, (zmin, zmax) in zones.items():
                if zone_name in fired:
                    continue
                if not (zmin <= ask <= zmax):
                    continue

                # Prior-K gate
                if prior_filter:
                    prior = candles[max(0, i - PRIOR_K) : i]
                    if len(prior) < PRIOR_K:
                        continue
                    prior_asks = [candle_ask_cents(pc, side) for pc in prior[-PRIOR_K:]]
                    if any(pa is None or pa < PRIOR_MIN_CENTS for pa in prior_asks):
                        continue

                won = result == side
                zone_outcomes[zone_name] = {
                    "won":      won,
                    "ask":      ask,
                    "profit":   pnl(won, ask),
                    "side":     side,
                    "secs_left": int(secs_left),
                    "ticker":   ticker,
                }
                fired.add(zone_name)
                break  # don't try NO if YES triggered for this zone

    return zone_outcomes


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",             type=int, default=14)
    ap.add_argument("--series",           nargs="+", default=SERIES_LIST)
    ap.add_argument("--out",              default="backtest_ask_floor.csv")
    ap.add_argument("--no-prior-filter",  action="store_true",
                    help="Disable prior-candle gate — raw zone WR only")
    ap.add_argument("--buckets",          action="store_true",
                    help="Split 90-95c into individual cent buckets")
    args = ap.parse_args()

    use_prior = not args.no_prior_filter
    zones     = ZONES_BUCKET if args.buckets else ZONES
    cutoff    = int(time.time()) - args.days * 86400
    label     = "WITH prior filter" if use_prior else "NO prior filter (raw)"
    mode      = "per-cent buckets" if args.buckets else "broad zones"
    print(f"Backtesting last {args.days} days across {len(args.series)} series  [{label}] [{mode}]")
    print(f"Break-even WR by entry price:")
    for name, (zmin, zmax) in zones.items():
        mid = (zmin + zmax) // 2
        print(f"  {name}: BE={breakeven_wr(mid)}%")
    print()

    stats = {z: {"n": 0, "wins": 0, "profit": 0.0, "asks": []} for z in zones}
    rows  = []

    for series in args.series:
        print(f"[{series}] fetching settled markets...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        print(f"[{series}] {len(markets)} markets — simulating...", flush=True)

        for idx, m in enumerate(markets):
            if idx % 100 == 0:
                print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)
            outcomes = simulate(m, prior_filter=use_prior, zones=zones)
            for zone_name, o in outcomes.items():
                stats[zone_name]["n"]      += 1
                stats[zone_name]["wins"]   += int(o["won"])
                stats[zone_name]["profit"] += o["profit"]
                stats[zone_name]["asks"].append(o["ask"])
                rows.append({
                    "series":        series,
                    "ticker":        o["ticker"],
                    "zone":          zone_name,
                    "side":          o["side"],
                    "ask":           o["ask"],
                    "secs_left":     o["secs_left"],
                    "won":           o["won"],
                    "profit":        o["profit"],
                    "prior_filter":  use_prior,
                })
        print(f"[{series}] done ({len(markets)} markets)          ", flush=True)

    print("\n" + "=" * 65)
    print(f"RESULTS  ({args.days}d, {len(args.series)} series, ${BET_SIZE} bets)  [{label}]")
    print("=" * 65)
    print(f"{'Zone':<15} {'n':>6} {'WR':>8} {'BE-WR':>8} {'Net P&L':>10} {'AvgAsk':>8}")
    print("-" * 65)
    for name in zones:
        s = stats[name]
        n = s["n"]
        if n == 0:
            print(f"{name:<15} {'0':>6}  {'—':>7}  {'—':>7}  {'—':>9}  {'—':>7}")
            continue
        wr      = s["wins"] / n * 100
        avg_ask = sum(s["asks"]) / len(s["asks"])
        be      = breakeven_wr(int(avg_ask))
        edge    = wr - be
        print(f"{name:<15} {n:>6}  {wr:>6.1f}%  {be:>6.1f}%  ${s['profit']:>+8.0f}"
              f"  {avg_ask:>6.1f}c  (edge {edge:+.1f}pp)")
    print("=" * 65)

    if rows:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nRaw trades saved to {args.out}")


if __name__ == "__main__":
    main()
