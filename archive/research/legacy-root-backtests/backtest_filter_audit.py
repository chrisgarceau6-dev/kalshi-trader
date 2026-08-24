#!/usr/bin/env python3
"""Filter audit: test all live strategy filters in a single pass.

Fetches candles once per market, evaluates every filter configuration
simultaneously. Shows volume change and WR vs baseline so we know
which filters are earning their keep.

Baseline = current live: PRIOR_N=3, PRIOR_MIN=80c, [90,93]c, 150-600s

Run: python3 backtest_filter_audit.py [--days 60]
"""

import csv, time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
BLACKOUT    = {15, 16, 17}
FEE         = 0.07
BET         = 45

# All configs evaluated simultaneously per market
CONFIGS = {
    "baseline":    dict(prior_n=3, prior_min=80, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "prior_n=2":   dict(prior_n=2, prior_min=80, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "prior_n=1":   dict(prior_n=1, prior_min=80, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "no_prior":    dict(prior_n=0, prior_min=80, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "prior_min=75":dict(prior_n=3, prior_min=75, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "prior_min=70":dict(prior_n=3, prior_min=70, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "time_120-700":dict(prior_n=3, prior_min=80, entry_min=90, entry_max=93, min_secs=120, max_secs=700),
    "time_100-800":dict(prior_n=3, prior_min=80, entry_min=90, entry_max=93, min_secs=100, max_secs=800),
    "ask_max=95":  dict(prior_n=3, prior_min=80, entry_min=90, entry_max=95, min_secs=150, max_secs=600),
    "n2+min75":    dict(prior_n=2, prior_min=75, entry_min=90, entry_max=93, min_secs=150, max_secs=600),
    "n2+120-700":  dict(prior_n=2, prior_min=80, entry_min=90, entry_max=93, min_secs=120, max_secs=700),
}
BASELINE = "baseline"


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


def check_prior(candles, i, side, prior_n, prior_min):
    if prior_n == 0:
        return True
    prior = candles[max(0, i - prior_n):i]
    if len(prior) < prior_n:
        return False
    return all(candle_price(p, side) is not None and candle_price(p, side) >= prior_min for p in prior)


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
                if close_dt.hour not in BLACKOUT:
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
        return {}

    candles = fetch_candles(ticker, series, close_ts)
    if not candles:
        return {}

    # Per-config results: {config_name: [rows]}
    results = {name: [] for name in CONFIGS}
    fired   = {name: set() for name in CONFIGS}

    # Broadest possible window — filter per config inside loop
    min_secs_global = min(c["min_secs"] for c in CONFIGS.values())
    max_secs_global = max(c["max_secs"] for c in CONFIGS.values())
    entry_max_global = max(c["entry_max"] for c in CONFIGS.values())
    entry_min_global = min(c["entry_min"] for c in CONFIGS.values())

    for i, c in enumerate(candles):
        end_ts    = c.get("end_period_ts", 0)
        secs_left = close_ts - end_ts
        if not (min_secs_global <= secs_left <= max_secs_global):
            continue

        for side in ("yes", "no"):
            ask = candle_price(c, side)
            if ask is None or not (entry_min_global <= ask <= entry_max_global):
                continue

            won = result == side

            for name, cfg in CONFIGS.items():
                if side in fired[name]:
                    continue
                if not (cfg["min_secs"] <= secs_left <= cfg["max_secs"]):
                    continue
                if not (cfg["entry_min"] <= ask <= cfg["entry_max"]):
                    continue
                if not check_prior(candles, i, side, cfg["prior_n"], cfg["prior_min"]):
                    continue

                results[name].append({
                    "series":    series,
                    "ticker":    ticker,
                    "side":      side,
                    "ask":       ask,
                    "secs_left": int(secs_left),
                    "won":       won,
                    "profit":    trade_pnl(won, ask),
                })
                fired[name].add(side)

    return results


def summarize(rows):
    if not rows:
        return 0, 0.0, 0.0
    n    = len(rows)
    wins = sum(1 for r in rows if r["won"])
    pnl  = sum(r["profit"] for r in rows)
    return n, wins / n * 100, pnl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out",  default="backtest_filter_audit.csv")
    args = ap.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"Filter audit — {args.days}d, {len(SERIES_LIST)} series, {len(CONFIGS)} configs")
    print(f"Excluding blackout hours {sorted(BLACKOUT)} UTC\n")

    all_results = {name: [] for name in CONFIGS}
    series_times = []

    for series in SERIES_LIST:
        t0 = time.time()
        print(f"[{series}] fetching...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        print(f"[{series}] {len(markets)} markets", flush=True)
        for idx, m in enumerate(markets):
            if idx % 100 == 0:
                elapsed = time.time() - t0
                if series_times and idx > 0:
                    avg_min = sum(series_times) / len(series_times)
                    remaining = (len(SERIES_LIST) - len(series_times) - 1) * avg_min + (len(markets) - idx) / max(idx, 1) * elapsed / 60
                    print(f"  {idx}/{len(markets)}  ~{remaining:.0f}min left", end="\r", flush=True)
                else:
                    print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)
            market_results = simulate(m)
            for name, rows in market_results.items():
                all_results[name].extend(rows)

        elapsed = time.time() - t0
        series_times.append(elapsed / 60)
        remaining = (len(SERIES_LIST) - len(series_times)) * (sum(series_times) / len(series_times))
        print(f"[{series}] done — {elapsed/60:.1f}min (~{remaining:.0f}min left)          ")

    # Save baseline CSV
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series","ticker","side","ask","secs_left","won","profit"])
        w.writeheader()
        w.writerows(all_results[BASELINE])

    # Print comparison table
    base_n, base_wr, base_pnl = summarize(all_results[BASELINE])
    print(f"\n{'Config':<16} {'n':>6}  {'vs base':>8}  {'WR':>7}  {'WR diff':>8}  {'P&L':>10}  {'$/trade':>8}")
    print("-" * 76)
    for name in CONFIGS:
        n, wr, pnl = summarize(all_results[name])
        vol_diff = f"{(n - base_n) / max(base_n, 1) * 100:+.1f}%"
        wr_diff  = f"{wr - base_wr:+.1f}pp" if base_n else "—"
        marker   = " ◀ CURRENT" if name == BASELINE else ""
        print(f"{name:<16} {n:>6}  {vol_diff:>8}  {wr:>6.1f}%  {wr_diff:>8}  ${pnl:>+9.0f}  ${pnl/max(n,1):>+7.2f}/trade{marker}")

    print(f"\nBaseline: {base_n} trades, {base_wr:.1f}% WR, ${base_pnl:+.0f} P&L")
    print(f"Saved baseline rows to {args.out}")


if __name__ == "__main__":
    main()
