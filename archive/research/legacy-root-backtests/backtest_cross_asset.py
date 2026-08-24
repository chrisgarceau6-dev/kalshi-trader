#!/usr/bin/env python3
"""Backtest: cross-asset lead-lag confirmation filter.

Tests whether a 85-89c entry on Asset B is more likely to win when
at least one companion asset is simultaneously at 90c+ on the same side.

Logic: all crypto 15-min series close at the same clock time (e.g. 21:45 ET).
At any given minute T, we can check all other series' asks. If BTC is at
92c YES while SOL is at 86c YES, BTC acts as a leading indicator — the
broad market move is already confirmed at high certainty.

Run:
    python3 backtest_cross_asset.py            # 14 days
    python3 backtest_cross_asset.py --days 60  # full window (slow, ~2h)

Output:
    - WR for 85-89c WITH companion at 90c+  vs WITHOUT
    - P&L comparison
    - Per-series breakdown
"""

import csv, os, sys, time, argparse
from datetime import datetime, timezone
from collections import defaultdict
from kalshi_auth import get as kalshi_get

SERIES_LIST = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M",
    "KXBNB15M", "KXXRP15M", "KXHYPE15M", "KXNEAR15M",
]

PRIOR_MIN_CENTS  = 80
PRIOR_K          = 3
MIN_SECS_LEFT    = 150
MAX_SECS_LEFT    = 600
BLACKOUT_HOURS   = {15, 16, 17}  # UTC
FEE              = 0.07
BET_SIZE         = 35

TARGET_MIN       = 85   # the zone we're testing (currently skipped by live trader)
TARGET_MAX       = 89
COMPANION_MIN    = 90   # "confirmed" = at least one companion series at 90c+
COMPANION_MAX    = 95

# Max seconds between candle timestamps to count as "same time"
COMPANION_WINDOW_S = 90


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
    return round(contracts * (1.0 - ask_cents / 100) * (1 - FEE), 2) if won else -BET_SIZE


def fetch_settled_markets(series, cutoff_ts):
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        code, r = kalshi_get("/markets", params)
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
    code, r = kalshi_get(
        f"/series/{series}/markets/{ticker}/candlesticks",
        {"start_ts": close_ts - 900, "end_ts": close_ts + 30, "period_interval": 1},
    )
    if code != 200:
        return []
    return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",  type=int, default=14)
    ap.add_argument("--series", nargs="+", default=SERIES_LIST)
    ap.add_argument("--out",   default="backtest_cross_asset.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Cross-asset lead-lag backtest — last {args.days} days, {len(args.series)} series")
    print(f"Testing: enter {TARGET_MIN}-{TARGET_MAX}c only when companion series at {COMPANION_MIN}c+")
    print()

    # ── Step 1: collect all markets grouped by 15-min window ─────────────────
    print("Fetching settled markets...", flush=True)
    # window_ts = close_ts rounded to nearest 15 min (900s)
    windows = defaultdict(dict)  # {window_ts: {series: market}}

    for series in args.series:
        markets = fetch_settled_markets(series, cutoff)
        for m in markets:
            close_ts = parse_close_ts(m)
            w = round(close_ts / 900) * 900
            windows[w][series] = m
        print(f"  {series}: {len(markets)} markets", flush=True)

    print(f"\n{len(windows)} time windows to process\n", flush=True)

    # ── Step 2: for each window, fetch all candles and run cross-asset sim ────
    # Stats: split by companion presence
    stat = {
        "with_companion":    {"n": 0, "wins": 0, "profit": 0.0},
        "without_companion": {"n": 0, "wins": 0, "profit": 0.0},
        "current_zone":      {"n": 0, "wins": 0, "profit": 0.0},  # 90-95c ref
    }
    rows = []

    for widx, (window_ts, series_markets) in enumerate(sorted(windows.items())):
        if widx % 50 == 0:
            print(f"  window {widx}/{len(windows)}", end="\r", flush=True)

        # Skip blackout windows
        wdt = datetime.fromtimestamp(window_ts, tz=timezone.utc)
        if wdt.hour in BLACKOUT_HOURS:
            continue

        # Fetch candles for every series present in this window
        series_candles = {}  # {series: [candles]}
        for series, market in series_markets.items():
            close_ts = parse_close_ts(market)
            candles = fetch_candles(market["ticker"], series, close_ts)
            if candles:
                series_candles[series] = (candles, close_ts, market.get("result", ""))
            time.sleep(0.06)

        # Build companion ask index: {minute_ts: {series: {side: ask}}}
        # Only include candles in the trigger window
        companion_idx = defaultdict(dict)
        for series, (candles, close_ts, _) in series_candles.items():
            for c in candles:
                end_ts    = c.get("end_period_ts", 0)
                secs_left = close_ts - end_ts
                if not (MIN_SECS_LEFT <= secs_left <= MAX_SECS_LEFT):
                    continue
                yes_ask = candle_ask_cents(c, "yes")
                no_ask  = candle_ask_cents(c, "no")
                companion_idx[end_ts][series] = {"yes": yes_ask, "no": no_ask}

        # Evaluate each series for 85-89c triggers + companion check
        for series, (candles, close_ts, result) in series_candles.items():
            if not result:
                continue

            fired = False  # one trigger per market per series
            for i, c in enumerate(candles):
                if fired:
                    break
                end_ts    = c.get("end_period_ts", 0)
                secs_left = close_ts - end_ts
                if not (MIN_SECS_LEFT <= secs_left <= MAX_SECS_LEFT):
                    continue

                for side in ("yes", "no"):
                    ask = candle_ask_cents(c, side)
                    if ask is None:
                        continue

                    # ── Reference zone (90-95c, current live) ──────────────
                    if COMPANION_MIN <= ask <= COMPANION_MAX:
                        prior = candles[max(0, i - PRIOR_K): i]
                        if len(prior) >= PRIOR_K:
                            p_asks = [candle_ask_cents(pc, side) for pc in prior[-PRIOR_K:]]
                            if all(pa is not None and pa >= PRIOR_MIN_CENTS for pa in p_asks):
                                won = result == side
                                stat["current_zone"]["n"]      += 1
                                stat["current_zone"]["wins"]   += int(won)
                                stat["current_zone"]["profit"] += pnl(won, ask)
                        break  # don't double-count YES + NO for ref zone

                    # ── Target zone (85-89c) ────────────────────────────────
                    if not (TARGET_MIN <= ask <= TARGET_MAX):
                        continue

                    prior = candles[max(0, i - PRIOR_K): i]
                    if len(prior) < PRIOR_K:
                        continue
                    p_asks = [candle_ask_cents(pc, side) for pc in prior[-PRIOR_K:]]
                    if any(pa is None or pa < PRIOR_MIN_CENTS for pa in p_asks):
                        continue

                    # Check companion: any OTHER series at 90c+ at same minute (±90s)
                    has_companion = False
                    for ts_key, ts_series in companion_idx.items():
                        if abs(ts_key - end_ts) > COMPANION_WINDOW_S:
                            continue
                        for comp_series, comp_asks in ts_series.items():
                            if comp_series == series:
                                continue
                            comp_ask = comp_asks.get(side)
                            if comp_ask is not None and comp_ask >= COMPANION_MIN:
                                has_companion = True
                                break
                        if has_companion:
                            break

                    won     = result == side
                    profit  = pnl(won, ask)
                    bucket  = "with_companion" if has_companion else "without_companion"
                    stat[bucket]["n"]      += 1
                    stat[bucket]["wins"]   += int(won)
                    stat[bucket]["profit"] += profit
                    rows.append({
                        "window_ts":     window_ts,
                        "series":        series,
                        "ticker":        series_markets[series]["ticker"],
                        "side":          side,
                        "ask":           ask,
                        "secs_left":     int(secs_left),
                        "has_companion": has_companion,
                        "won":           won,
                        "profit":        profit,
                        "result":        result,
                    })
                    fired = True
                    break  # YES takes priority, stop side loop

    print(f"\n{'─'*60}", flush=True)

    # ── Results ───────────────────────────────────────────────────────────────
    print(f"\nRESULTS  ({args.days}d, {len(args.series)} series, ${BET_SIZE} bets)\n")

    def print_row(label, s):
        n = s["n"]
        if n == 0:
            print(f"  {label:<30}  n=0")
            return
        wr = s["wins"] / n * 100
        print(f"  {label:<30}  n={n:>5}  WR={wr:>5.1f}%  P&L=${s['profit']:>+8.0f}")

    print(f"{'─'*60}")
    print_row("90-95c  (current live zone)", stat["current_zone"])
    print(f"{'─'*60}")
    print_row("85-89c WITH companion 90c+",  stat["with_companion"])
    print_row("85-89c WITHOUT companion",    stat["without_companion"])
    print(f"{'─'*60}")

    wc = stat["with_companion"]
    woc = stat["without_companion"]
    if wc["n"] > 0 and woc["n"] > 0:
        wr_c  = wc["wins"]  / wc["n"]  * 100
        wr_nc = woc["wins"] / woc["n"] * 100
        print(f"\n  WR lift from companion filter: {wr_c - wr_nc:+.1f}pp")
        print(f"  ({wr_c:.1f}% with vs {wr_nc:.1f}% without)")

    print(f"\nDeploy threshold for 85-89c: WR > 88% + 2pp safety = 90%+")
    print(f"If 'with_companion' WR >= 90%: filter is potentially worth deploying.\n")

    if rows:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"Raw trades saved to {args.out}")


if __name__ == "__main__":
    main()
