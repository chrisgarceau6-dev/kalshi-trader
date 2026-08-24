#!/usr/bin/env python3
"""Hourly market backtest — KXWTIH, KXDJI, KXPALLADIUMH, KXPLATINUMH.

Tests late-certainty strategy on 1-hour markets with scaled entry windows.
15M equivalent (150-600s out of 900s) → 1H equivalent (600-2400s out of 3600s).
Tests a range so we find the actual sweet spot.

Run: python3 backtest_hourly.py [--days 30] [--series KXWTIH KXDJI]
"""

import time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

DEFAULT_SERIES = ["KXWTIH", "KXDJI", "KXPALLADIUMH", "KXPLATINUMH"]
FEE    = 0.07
BET    = 45
OOS_FRAC = 0.33

# Time windows to test — 15M uses 150-600s; for 1H we scale and also try equivalent
TIME_WINDOWS = [
    (60,  300,  "60-300s  (ultra-late)"),
    (120, 600,  "120-600s (tight)"),
    (150, 600,  "150-600s (crypto-live)"),
    (200, 900,  "200-900s"),
    (300, 900,  "300-900s"),
    (300, 1200, "300-1200s"),
    (600, 1800, "600-1800s (1H-equiv)"),
    (600, 2400, "600-2400s (1H-wide)"),
]

ASK_GATES = [
    (88, 93, "88-93c"),
    (90, 93, "90-93c (live)"),
    (90, 95, "90-95c"),
    (91, 95, "91-95c"),
    (93, 97, "93-97c"),
]

PRIORS = [
    (0, 0,  "no_prior"),
    (2, 75, "n=2,min=75 (live)"),
]


def candle_yes_ask(c):
    try:
        return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
    except (KeyError, ValueError, TypeError):
        return None

def trade_pnl(won, ask_cents):
    contracts = BET / (ask_cents / 100)
    return round(contracts * (1.0 - ask_cents / 100) * (1 - FEE), 2) if won else -BET

def prior_ok(candles, i, n, min_c):
    if n == 0:
        return True
    window = candles[max(0, i-n):i]
    if len(window) < n:
        return False
    return all(candle_yes_ask(c) is not None and candle_yes_ask(c) >= min_c for c in window)

def parse_close_ts(m):
    ct = m.get("close_time", "")
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0

def fetch_markets(series, cutoff_ts):
    markets, cursor, pages = [], None, 0
    while pages < 20:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            code, r = kalshi_get("/markets", params)
        except Exception:
            time.sleep(2); continue
        if code != 200 or not r:
            break
        batch = r.get("markets", [])
        if not batch:
            break
        stopped = False
        for m in batch:
            ts = parse_close_ts(m)
            if ts and ts < cutoff_ts:
                stopped = True; break
            if ts:
                markets.append({"ticker": m["ticker"], "ts": ts,
                                "won": m.get("result") == "yes"})
        pages += 1
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return sorted(markets, key=lambda m: m["ts"])

def fetch_candles(ticker, series, close_ts, lookback=4000):
    for attempt in range(3):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - lookback, "end_ts": close_ts + 10, "period_interval": 1},
            )
            if code == 200:
                return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return []

def load_dataset(markets, series, lookback=4000):
    out = []
    for i, m in enumerate(markets):
        c = fetch_candles(m["ticker"], series, m["ts"], lookback)
        out.append((m, c))
        if (i+1) % 200 == 0:
            print(f"    candles {i+1}/{len(markets)}...", flush=True)
        time.sleep(0.015)
    return out


def run_grid(dataset, oos_split, days_actual):
    """Evaluate all (ask gate × prior × time window) configs. Return results sorted by OOS $/trade."""
    configs = []
    for (alo, ahi, albl) in ASK_GATES:
        for (pn, pm, plbl) in PRIORS:
            for (tlo, thi, tlbl) in TIME_WINDOWS:
                all_r, oos_r = [], []
                for idx, (market, candles) in enumerate(dataset):
                    won = market["won"]
                    for i, c in enumerate(candles):
                        secs = market["ts"] - c.get("end_period_ts", 0)
                        if not (tlo <= secs <= thi):
                            continue
                        ask = candle_yes_ask(c)
                        if ask is None or not (alo <= ask <= ahi):
                            continue
                        if not prior_ok(candles, i, pn, pm):
                            continue
                        pnl = trade_pnl(won, ask)
                        all_r.append(pnl)
                        if idx >= oos_split:
                            oos_r.append(pnl)
                        break

                configs.append({
                    "ask": albl, "prior": plbl, "time": tlbl,
                    "all_n":   len(all_r),
                    "all_wr":  sum(1 for p in all_r if p>0)/len(all_r)*100 if all_r else 0,
                    "all_ppt": sum(all_r)/len(all_r) if all_r else -999,
                    "all_ppd": sum(all_r)/days_actual if all_r else -999,
                    "oos_n":   len(oos_r),
                    "oos_wr":  sum(1 for p in oos_r if p>0)/len(oos_r)*100 if oos_r else 0,
                    "oos_ppt": sum(oos_r)/len(oos_r) if oos_r else -999,
                    "oos_ppd": sum(oos_r)/(days_actual*OOS_FRAC) if oos_r else -999,
                })

    valid = [c for c in configs if c["oos_n"] >= 10]
    valid.sort(key=lambda c: c["oos_ppt"], reverse=True)
    return valid


def print_results(results, label, top_n=12):
    print(f"\n  {label}")
    print(f"  {'Ask':10s} {'Prior':16s} {'Time window':16s} {'All n':>5} {'WR':>7} {'$/tr':>7} │ {'OOS n':>5} {'WR':>7} {'$/tr':>7} {'$/day':>8}")
    print("  " + "-"*100)
    for r in results[:top_n]:
        live = " ◀" if r["ask"]=="90-93c (live)" and r["prior"]=="n=2,min=75 (live)" else ""
        print(f"  {r['ask']:10s} {r['prior']:16s} {r['time']:16s} "
              f"{r['all_n']:>5} {r['all_wr']:>6.1f}% {r['all_ppt']:>+7.2f} │ "
              f"{r['oos_n']:>5} {r['oos_wr']:>6.1f}% {r['oos_ppt']:>+7.2f} {r['oos_ppd']:>+8.2f}{live}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int, default=30)
    parser.add_argument("--series", nargs="*", default=None)
    args = parser.parse_args()

    cutoff  = int(time.time()) - args.days * 86400
    to_test = args.series or DEFAULT_SERIES

    print(f"\nHourly market backtest — {args.days}d window, BET=${BET}, FEE={int(FEE*100)}%")
    print(f"{'='*70}")

    summary = []

    for series in to_test:
        print(f"\n[{series}] Fetching markets...", flush=True)
        markets = fetch_markets(series, cutoff)
        n = len(markets)
        if n == 0:
            print(f"  No data."); continue

        times = [m["ts"] for m in markets]
        t0 = datetime.fromtimestamp(min(times), tz=timezone.utc).strftime("%b %d")
        t1 = datetime.fromtimestamp(max(times), tz=timezone.utc).strftime("%b %d")
        days_actual = max((max(times) - min(times)) / 86400, 1)
        yes_rate = sum(1 for m in markets if m["won"]) / n
        print(f"  {n} markets  {t0}→{t1}  ({days_actual:.1f}d)  YES%={yes_rate:.0%}  ~{n/days_actual:.0f}/day")

        oos_split = int(n * (1 - OOS_FRAC))
        print(f"  Train: {oos_split}  OOS: {n-oos_split}")
        print(f"  Loading candles...", flush=True)

        # For hourly markets use 4000s lookback to cover the 600-2400s window
        dataset = load_dataset(markets, series, lookback=4000)

        results = run_grid(dataset, oos_split, days_actual)

        if not results:
            print(f"  No configs reached minimum 10 OOS trades."); continue

        print_results(results, f"Top configs by OOS $/trade (min 10 OOS trades)")

        best = results[0]
        summary.append({
            "series": series,
            "best_config": f"ask={best['ask']} {best['prior']} {best['time']}",
            "oos_wr": best["oos_wr"],
            "oos_ppt": best["oos_ppt"],
            "oos_ppd": best["oos_ppd"],
            "oos_n": best["oos_n"],
            "all_ppd": best["all_ppd"],
        })

    if summary:
        crypto_ppd = 52.0
        print(f"\n{'='*70}")
        print(f"SUMMARY — best OOS config per series")
        print(f"{'Series':20s} {'OOS WR':>7} {'OOS $/tr':>9} {'OOS $/day':>10} {'n':>5}")
        print("-"*60)
        total_add = 0
        for s in summary:
            flag = " ✓" if s["oos_ppt"] > 0 else " ✗"
            print(f"  {s['series']:18s} {s['oos_wr']:>6.1f}%  {s['oos_ppt']:>+8.2f}  {s['oos_ppd']:>+9.2f}  {s['oos_n']:>4}{flag}")
            if s["oos_ppt"] > 0:
                total_add += s["oos_ppd"]
        print("-"*60)
        print(f"  Current strategy (crypto+WTI15M):         ~+${crypto_ppd:.0f}/day")
        print(f"  Hourly additions (positive OOS only):     ~{total_add:>+.0f}/day")
        print(f"  Combined:                                 ~+${crypto_ppd+total_add:.0f}/day")


if __name__ == "__main__":
    main()
