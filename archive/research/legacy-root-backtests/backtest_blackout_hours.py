#!/usr/bin/env python3
"""Backtest: WR during blackout hours (UTC 15, 16, 17 = 11am-1pm ET).

Tests whether the equity-open blackout is actually hurting performance.
Uses identical entry logic to live strategy (90-95c, 150-600s, 3 prior ≥80c)
but only processes markets that close during hours 15, 16, 17 UTC.

If WR during those hours matches overall (~96%), blackout is unnecessary.

Run: python3 backtest_blackout_hours.py [--days 60]
"""

import csv, time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

ENTRY_MIN  = 90
ENTRY_MAX  = 95
MIN_SECS   = 150
MAX_SECS   = 600
PRIOR_MIN  = 80
PRIOR_N    = 3
BLACKOUT   = {15, 16, 17}
FEE        = 0.07
BET        = 45


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
                close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
                if close_dt.hour in BLACKOUT:
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
    hour     = close_dt.hour

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
            if any(candle_price(p, side) is None or candle_price(p, side) < PRIOR_MIN for p in prior):
                continue

            won = result == side
            outcomes.append({
                "series":    series,
                "ticker":    ticker,
                "side":      side,
                "ask":       ask,
                "hour_utc":  hour,
                "secs_left": int(secs_left),
                "won":       won,
                "profit":    trade_pnl(won, ask),
            })
            fired.add(side)

    return outcomes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out",  default="backtest_blackout_hours.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Blackout-hours backtest — {args.days}d, {len(SERIES_LIST)} series")
    print(f"Testing UTC hours: {sorted(BLACKOUT)} (11am-1pm ET, equity open)")
    print(f"Entry: [{ENTRY_MIN},{ENTRY_MAX}]c, {MIN_SECS}-{MAX_SECS}s, {PRIOR_N} prior ≥{PRIOR_MIN}c\n")

    rows = []
    series_times = []
    for series in SERIES_LIST:
        t0 = time.time()
        print(f"[{series}] fetching...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        print(f"[{series}] {len(markets)} blackout markets", flush=True)
        for idx, m in enumerate(markets):
            if idx % 50 == 0:
                print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)
            rows.extend(simulate(m))
        elapsed = time.time() - t0
        series_times.append(elapsed / 60)
        remaining = len(SERIES_LIST) - len(series_times)
        avg_min   = sum(series_times) / len(series_times)
        print(f"[{series}] done — {elapsed/60:.1f}min (~{remaining * avg_min:.0f}min left)          ")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series","ticker","side","ask","hour_utc","secs_left","won","profit"])
        w.writeheader()
        w.writerows(rows)

    if not rows:
        print("No trades found in blackout hours.")
        return

    n    = len(rows)
    wins = sum(1 for r in rows if r["won"])
    pnl  = sum(r["profit"] for r in rows)
    wr   = wins / n * 100
    print(f"\nBlackout hours overall: n={n}, WR={wr:.1f}%, P&L=${pnl:+.0f}, ${pnl/n:+.2f}/trade")
    print(f"Break-even WR range: {breakeven_wr(90):.1f}% (90c) – {breakeven_wr(95):.1f}% (95c)")

    print(f"\n{'Hour UTC':<10} {'n':>6}  {'WR':>7}  {'Net P&L':>10}  {'$/trade':>8}")
    print("-" * 48)
    for h in sorted(BLACKOUT):
        subset = [r for r in rows if r["hour_utc"] == h]
        if not subset:
            print(f"UTC {h:<6} {'—':>6}")
            continue
        hn  = len(subset)
        hw  = sum(1 for r in subset if r["won"])
        hp  = sum(r["profit"] for r in subset)
        hwr = hw / hn * 100
        print(f"UTC {h:<6} {hn:>6}  {hwr:>6.1f}%  ${hp:>+10.0f}  ${hp/hn:>+7.2f}/trade")

    print(f"\n{'Series':<14} {'n':>6}  {'WR':>7}  {'Net P&L':>10}")
    print("-" * 44)
    for s in SERIES_LIST:
        subset = [r for r in rows if r["series"] == s]
        if not subset:
            continue
        sn = len(subset)
        sw = sum(1 for r in subset if r["won"])
        sp = sum(r["profit"] for r in subset)
        print(f"{s:<14} {sn:>6}  {sw/sn*100:>6.1f}%  ${sp:>+10.0f}")

    verdict = "SAFE TO REMOVE" if wr >= 94.5 else "KEEP BLACKOUT"
    print(f"\nVerdict: {verdict} (WR={wr:.1f}%, BE≈94.5%)")
    print(f"Saved to {args.out}")


if __name__ == "__main__":
    main()
