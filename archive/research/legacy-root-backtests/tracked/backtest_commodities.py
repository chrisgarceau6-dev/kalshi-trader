#!/usr/bin/env python3
"""Commodity series backtest — KXWTI15M, KXGOLD15M, KXSILVER15M.

Checks data availability then runs YES-only late-certainty strategy
against the current live config and key variations, with a 67/33 OOS split.

Run: python3 backtest_commodities.py [--days 90] [--series KXWTI15M KXGOLD15M]
"""

import time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

COMMODITY_SERIES = ["KXWTI15M", "KXGOLD15M", "KXSILVER15M"]

FEE = 0.07
BET = 45

# Live config + variations to understand the dynamics
CONFIGS = {
    "live (n=2,min=75)": dict(prior_n=2, prior_min=75, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "no_prior":          dict(prior_n=0, prior_min=75, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "prior_n=1,min=75":  dict(prior_n=1, prior_min=75, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "prior_n=3,min=75":  dict(prior_n=3, prior_min=75, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "wider_ask_90-95":   dict(prior_n=2, prior_min=75, entry_min=90, entry_max=95, min_secs=150, max_secs=600),
    "wider_time_120-700":dict(prior_n=2, prior_min=75, entry_min=90, entry_max=93, min_secs=120, max_secs=700),
}
LIVE = "live (n=2,min=75)"


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
    return round(contracts * (1.0 - ask_cents / 100) * (1 - FEE), 2) if won else -BET


def check_prior(candles, i, prior_n, prior_min):
    if prior_n == 0:
        return True
    prior = candles[max(0, i - prior_n):i]
    if len(prior) < prior_n:
        return False
    return all(candle_price(p, "yes") is not None and candle_price(p, "yes") >= prior_min for p in prior)


def parse_close_ts(m):
    ct = m.get("close_time", "")
    if not ct:
        return 0
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


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
        if code != 200 or not r:
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
                markets.append({
                    "ticker":   m["ticker"],
                    "close_ts": close_ts,
                    "won":      m.get("result", "") == "yes",
                    "close_dt": datetime.fromtimestamp(close_ts, tz=timezone.utc),
                })
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return sorted(markets, key=lambda m: m["close_ts"])


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
    return []


def simulate_market(market, series):
    ticker, close_ts, won = market["ticker"], market["close_ts"], market["won"]
    candles = fetch_candles(ticker, series, close_ts)
    if not candles:
        return {}
    results = {}
    for name, cfg in CONFIGS.items():
        results[name] = None
        for i, c in enumerate(candles):
            secs = close_ts - c.get("end_period_ts", 0)
            if not (cfg["min_secs"] <= secs <= cfg["max_secs"]):
                continue
            ask = candle_price(c, "yes")
            if ask is None or not (cfg["entry_min"] <= ask <= cfg["entry_max"]):
                continue
            if not check_prior(candles, i, cfg["prior_n"], cfg["prior_min"]):
                continue
            results[name] = trade_pnl(won, ask)
            break
    return results


def aggregate(results_list):
    agg = {name: {"pnl": 0.0, "wins": 0, "n": 0} for name in CONFIGS}
    for res in results_list:
        for name, pnl in res.items():
            if pnl is None:
                continue
            agg[name]["pnl"] += pnl
            agg[name]["wins"] += 1 if pnl > 0 else 0
            agg[name]["n"] += 1
    return agg


def print_table(agg, label, days):
    print(f"\n  {label}")
    print(f"  {'Config':<22} {'n':>5}  {'WR':>7}  {'Total P&L':>11}  {'$/trade':>8}  {'$/day':>8}")
    print(f"  {'-'*22}  {'-'*5}  {'-'*7}  {'-'*11}  {'-'*8}  {'-'*8}")
    for name in CONFIGS:
        r = agg[name]
        n = r["n"]
        if n == 0:
            print(f"  {name:<22} {'—':>5}")
            continue
        wr  = r["wins"] / n * 100
        ppt = r["pnl"] / n
        ppd = r["pnl"] / days
        live_flag = " ◀ LIVE" if name == LIVE else ""
        print(f"  {name:<22} {n:>5}  {wr:>6.1f}%  ${r['pnl']:>+10.2f}  {ppt:>+8.2f}  {ppd:>+8.2f}{live_flag}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int, default=90)
    parser.add_argument("--series", nargs="*", default=None)
    args = parser.parse_args()

    cutoff   = int(time.time()) - args.days * 86400
    to_test  = args.series or COMMODITY_SERIES
    oos_frac = 0.33

    print(f"\nCommodity backtest — {args.days}d window, YES-only, BET=${BET}")
    print(f"{'='*70}")

    for series in to_test:
        print(f"\n[{series}] Fetching settled markets...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        n = len(markets)

        if n == 0:
            print(f"  No data found — series may not exist or have no settled markets.")
            continue

        yes_rate = sum(1 for m in markets if m["won"]) / n
        t0 = markets[0]["close_dt"].strftime("%b %d")
        t1 = markets[-1]["close_dt"].strftime("%b %d")
        print(f"  {n} settled markets  |  {t0} → {t1}  |  YES settlement rate: {yes_rate:.1%}")

        # Hour distribution
        hour_counts = {}
        for m in markets:
            h = m["close_dt"].hour
            hour_counts[h] = hour_counts.get(h, 0) + 1
        busy = sorted(hour_counts, key=hour_counts.get, reverse=True)[:5]
        print(f"  Busiest UTC hours: {busy}")

        if n < 100:
            print(f"  WARNING — only {n} markets. Results will have wide CIs.")

        split = int(n * (1 - oos_frac))
        train = markets[:split]
        oos   = markets[split:]
        print(f"  Train: {len(train)} markets ({train[0]['close_dt'].strftime('%b %d')} → {train[-1]['close_dt'].strftime('%b %d')})")
        print(f"  OOS:   {len(oos)} markets  ({oos[0]['close_dt'].strftime('%b %d')} → {oos[-1]['close_dt'].strftime('%b %d')})")
        print(f"\n  Running simulation...", flush=True)

        all_results = []
        oos_results = []
        for idx, market in enumerate(markets):
            res = simulate_market(market, series)
            all_results.append(res)
            if idx >= split:
                oos_results.append(res)
            if (idx + 1) % 100 == 0:
                print(f"    {idx+1}/{n}...", flush=True)
            time.sleep(0.02)

        all_agg = aggregate(all_results)
        oos_agg = aggregate(oos_results)

        print_table(all_agg, f"FULL {args.days}d ({n} markets)", args.days)
        print_table(oos_agg, f"OOS  last {int(args.days * oos_frac)}d ({len(oos)} markets)", int(args.days * oos_frac))
        print()


if __name__ == "__main__":
    main()
