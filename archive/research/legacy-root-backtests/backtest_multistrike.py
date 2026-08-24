#!/usr/bin/env python3
"""Multi-strike backtest — measures incremental P&L from taking additional
strike prices at the same close time when multiple qualify.

Current strategy takes ONE qualifying market per series per scan cycle. But
at any 15M close time, multiple strike prices may simultaneously be in the
90-93c range. This backtest asks: what's the 2nd (and 3rd) strike worth?

Groups markets by (series, close_time) → finds all triggers per cluster →
compares single-strike vs multi-strike P&L with OOS validation.

Run: python3 backtest_multistrike.py [--days 60] [--series KXBTC15M KXETH15M]
"""

import time, argparse, collections
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

DEFAULT_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

FEE      = 0.07
BET      = 45
OOS_FRAC = 0.33

# Live config
MIN_ASK   = 90
MAX_ASK   = 93
MIN_SECS  = 150
MAX_SECS  = 600
PRIOR_N   = 2
PRIOR_MIN = 75

MAX_STRIKES = 3  # measure up to 3 strikes per close-time cluster


def candle_yes_ask(c):
    try:
        return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
    except (KeyError, ValueError, TypeError):
        return None


def trade_pnl(won, ask_cents):
    contracts = BET / (ask_cents / 100)
    return round(contracts * (1.0 - ask_cents / 100) * (1 - FEE), 2) if won else -BET


def prior_ok(candles, i):
    window = candles[max(0, i - PRIOR_N):i]
    if len(window) < PRIOR_N:
        return False
    return all(candle_yes_ask(c) is not None and candle_yes_ask(c) >= PRIOR_MIN for c in window)


def parse_close_ts(m):
    ct = m.get("close_time", "")
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def fetch_markets(series, cutoff_ts):
    markets, cursor, pages = [], None, 0
    while pages < 40:
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
                markets.append({"ticker": m["ticker"], "ts": ts, "won": m.get("result") == "yes"})
        pages += 1
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return sorted(markets, key=lambda m: m["ts"])


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


def find_trigger_ask(candles, close_ts):
    """Return the ask_cents of the first qualifying candle, or None."""
    for i, c in enumerate(candles):
        secs = close_ts - c.get("end_period_ts", 0)
        if not (MIN_SECS <= secs <= MAX_SECS):
            continue
        ask = candle_yes_ask(c)
        if ask is None or not (MIN_ASK <= ask <= MAX_ASK):
            continue
        if not prior_ok(candles, i):
            continue
        return ask
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",   type=int, default=60)
    parser.add_argument("--series", nargs="*", default=None)
    args = parser.parse_args()

    cutoff  = int(time.time()) - args.days * 86400
    to_test = args.series or DEFAULT_SERIES

    print(f"\nMulti-strike backtest — {args.days}d window, BET=${BET}, FEE={int(FEE*100)}%")
    print(f"Config: ask=[{MIN_ASK},{MAX_ASK}]c, {MIN_SECS}-{MAX_SECS}s, prior n={PRIOR_N} min={PRIOR_MIN}c")
    print(f"{'='*70}")

    summary = []

    for series in to_test:
        print(f"\n[{series}] Fetching markets...", flush=True)
        markets = fetch_markets(series, cutoff)
        n = len(markets)
        if n == 0:
            print("  No data."); continue

        times = [m["ts"] for m in markets]
        t0 = datetime.fromtimestamp(min(times), tz=timezone.utc).strftime("%b %d")
        t1 = datetime.fromtimestamp(max(times), tz=timezone.utc).strftime("%b %d")
        days_actual = max((max(times) - min(times)) / 86400, 1)
        oos_days = days_actual * OOS_FRAC

        # Group by close_time
        by_ts = collections.defaultdict(list)
        for m in markets:
            by_ts[m["ts"]].append(m)
        close_times = sorted(by_ts.keys())
        n_clusters = len(close_times)
        oos_split = int(n_clusters * (1 - OOS_FRAC))

        avg_strikes = n / n_clusters if n_clusters else 0
        print(f"  {n} markets  {t0}→{t1}  ({days_actual:.1f}d)  {n_clusters} clusters  ~{avg_strikes:.1f} strikes/close")
        print(f"  Train clusters: {oos_split}  OOS clusters: {n_clusters - oos_split}")
        print(f"  Loading candles...", flush=True)

        candles_by_ticker = {}
        for idx, m in enumerate(markets):
            candles_by_ticker[m["ticker"]] = fetch_candles(m["ticker"], series, m["ts"])
            if (idx + 1) % 200 == 0:
                print(f"    candles {idx+1}/{n}...", flush=True)
            time.sleep(0.015)

        # Simulate — for each cluster find all triggers, sorted highest ask first
        # (highest ask = deepest in-the-money = most certain)
        strike_pnl = [[] for _ in range(MAX_STRIKES)]
        strike_oos  = [[] for _ in range(MAX_STRIKES)]
        cluster_trigger_counts = collections.Counter()

        for cidx, ts in enumerate(close_times):
            is_oos = cidx >= oos_split
            triggers = []
            for m in by_ts[ts]:
                candles = candles_by_ticker.get(m["ticker"], [])
                ask = find_trigger_ask(candles, ts)
                if ask is not None:
                    triggers.append((ask, m["won"]))

            # Highest ask first (most certain outcome)
            triggers.sort(key=lambda x: x[0], reverse=True)
            cluster_trigger_counts[len(triggers)] += 1

            for k, (ask, won) in enumerate(triggers[:MAX_STRIKES]):
                pnl = trade_pnl(won, ask)
                strike_pnl[k].append(pnl)
                if is_oos:
                    strike_oos[k].append(pnl)

        # How often do clusters have multiple triggers?
        n_with_1plus = sum(v for k, v in cluster_trigger_counts.items() if k >= 1)
        n_with_2plus = sum(v for k, v in cluster_trigger_counts.items() if k >= 2)
        n_with_3plus = sum(v for k, v in cluster_trigger_counts.items() if k >= 3)
        print(f"\n  Trigger frequency:")
        print(f"    Clusters with ≥1 trigger: {n_with_1plus:4d} / {n_clusters} ({n_with_1plus/n_clusters*100:.1f}%)")
        if n_with_1plus:
            print(f"    Of those, with ≥2 triggers: {n_with_2plus:4d} ({n_with_2plus/n_with_1plus*100:.1f}%)")
            print(f"    Of those, with ≥3 triggers: {n_with_3plus:4d} ({n_with_3plus/n_with_1plus*100:.1f}%)")

        print(f"\n  Strike breakdown (sorted by ask desc — highest certainty first):")
        print(f"  {'':10} {'All n':>6} {'WR':>7} {'$/tr':>7} {'$/day':>8} │ {'OOS n':>6} {'OOS WR':>7} {'$/tr':>7} {'$/day':>8}")
        print("  " + "-"*85)

        labels = ["1st strike", "2nd strike", "3rd strike"]
        oos_ppt_by_strike = []
        for k in range(MAX_STRIKES):
            all_r = strike_pnl[k]
            oos_r = strike_oos[k]
            if not all_r:
                break
            all_wr  = sum(1 for p in all_r if p > 0) / len(all_r) * 100
            all_ppt = sum(all_r) / len(all_r)
            all_ppd = sum(all_r) / days_actual
            oos_wr  = sum(1 for p in oos_r if p > 0) / len(oos_r) * 100 if oos_r else 0
            oos_ppt = sum(oos_r) / len(oos_r) if oos_r else 0
            oos_ppd = sum(oos_r) / oos_days if oos_r else 0
            oos_ppt_by_strike.append(oos_ppt)
            flag = " (current)" if k == 0 else ""
            print(f"  {labels[k]:10}{flag:9} {len(all_r):>6} {all_wr:>6.1f}% {all_ppt:>+7.2f} {all_ppd:>+8.2f} │ "
                  f"{len(oos_r):>6} {oos_wr:>6.1f}% {oos_ppt:>+7.2f} {oos_ppd:>+8.2f}")

        # Incremental value of 2nd and 3rd strikes
        extra_oos_pnl = [p for k in range(1, MAX_STRIKES) for p in strike_oos[k]]
        extra_oos_ppd = sum(extra_oos_pnl) / oos_days if extra_oos_pnl else 0
        base_oos_ppd  = sum(strike_oos[0]) / oos_days if strike_oos[0] else 0

        print(f"\n  OOS uplift from additional strikes: {extra_oos_ppd:+.2f}/day")
        print(f"  Note: 2nd/3rd strikes are 100% correlated with 1st (same close time, same underlying)")

        summary.append({
            "series": series,
            "base_oos_ppd": base_oos_ppd,
            "extra_oos_ppd": extra_oos_ppd,
            "n_with_2plus": n_with_2plus,
            "n_with_1plus": n_with_1plus,
        })

    if summary:
        print(f"\n{'='*70}")
        print(f"SUMMARY")
        print(f"  {'Series':15} {'Base $/day':>12} {'Extra $/day':>12} {'Clusters w/2+':>15}")
        print("  " + "-"*55)
        total_base = total_extra = 0.0
        for s in summary:
            pct = s["n_with_2plus"] / s["n_with_1plus"] * 100 if s["n_with_1plus"] else 0
            print(f"  {s['series']:15} {s['base_oos_ppd']:>+12.2f} {s['extra_oos_ppd']:>+12.2f} "
                  f"{s['n_with_2plus']:>6} / {s['n_with_1plus']:<6} ({pct:.0f}%)")
            total_base  += s["base_oos_ppd"]
            total_extra += s["extra_oos_ppd"]
        print("  " + "-"*55)
        print(f"  {'TOTAL':15} {total_base:>+12.2f} {total_extra:>+12.2f}")
        print(f"\n  Single-strike (all 6 series OOS):    {total_base:>+.2f}/day")
        print(f"  Multi-strike uplift (OOS):            {total_extra:>+.2f}/day")
        print(f"  Combined:                             {total_base + total_extra:>+.2f}/day")
        print(f"\n  WARNING: extra strikes are correlated — 2 BTC strikes = 1 risk event doubled.")
        print(f"  Raising MAX_CONCURRENT_POSITIONS would be required to realize this uplift.")


if __name__ == "__main__":
    main()
