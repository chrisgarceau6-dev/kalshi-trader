#!/usr/bin/env python3
"""Backtest per-series rolling WR kill switch on existing filter_audit data.

Uses the 5,683-trade baseline dataset. Parses settlement time from ticker,
then simulates: if a series drops below PAUSE_WR_THRESHOLD over the last
PAUSE_LOOKBACK trades in that series, skip it for PAUSE_HOURS.

Compares P&L and WR with vs without the kill switch across param combos.
"""

import csv
from datetime import datetime, timezone, timedelta
from itertools import product

CSV_FILE = "backtest_filter_audit.csv"

# Month abbrev → number
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def parse_ticker_time(ticker):
    """Parse settlement datetime from ticker like KXBTC15M-26AUG091815-15."""
    try:
        parts = ticker.split("-")
        # parts[1] = '26AUG091815', parts[2] = '15' (strike, ignore)
        raw = parts[1]                      # e.g. '26AUG091815'
        year  = 2000 + int(raw[:2])         # 26 → 2026
        mon   = MONTHS[raw[2:5]]            # AUG → 8
        day   = int(raw[5:7])               # 09
        hour  = int(raw[7:9])               # 18
        minute= int(raw[9:11])              # 15
        return datetime(year, mon, day, hour, minute, tzinfo=timezone.utc)
    except Exception:
        return None


def load_baseline():
    trades = []
    with open(CSV_FILE) as f:
        for row in csv.DictReader(f):
            t = parse_ticker_time(row["ticker"])
            if t is None:
                continue
            trades.append({
                "series": row["series"],
                "ticker": row["ticker"],
                "won":    row["won"] == "True",
                "profit": float(row["profit"]),
                "ts":     t,
            })
    trades.sort(key=lambda x: x["ts"])
    return trades


def simulate(trades, lookback, threshold, cooldown_hours):
    """Simulate per-series pause logic. Returns (n, wins, pnl, skipped)."""
    series_history = {}   # series → list of bool (results)
    series_paused  = {}   # series → resume_datetime

    n = wins = skipped = 0
    pnl = 0.0

    for t in trades:
        series = t["series"]
        ts     = t["ts"]

        # Check pause
        paused_until = series_paused.get(series)
        if paused_until and ts < paused_until:
            skipped += 1
            continue

        # Take the trade
        n    += 1
        won   = t["won"]
        pnl  += t["profit"] if won else -45.0   # ~$45 cost on loss
        if won:
            wins += 1

        # Record result
        hist = series_history.setdefault(series, [])
        hist.append(won)

        # Check if we should pause after recording
        if len(hist) >= lookback:
            window = hist[-lookback:]
            wr = sum(window) / lookback
            if wr < threshold:
                series_paused[series] = ts + timedelta(hours=cooldown_hours)

    return n, wins, pnl, skipped


def main():
    trades = load_baseline()
    print(f"Loaded {len(trades)} trades  ({trades[0]['ts'].date()} → {trades[-1]['ts'].date()})")

    # Baseline (no pause)
    n0, w0, pnl0, _ = simulate(trades, lookback=999999, threshold=0.0, cooldown_hours=0)
    print(f"\nBaseline (no pause): {n0} trades  {w0/n0*100:.1f}% WR  ${pnl0:+.0f}\n")

    # Param grid
    lookbacks      = [15, 20, 25]
    thresholds     = [0.80, 0.83, 0.85, 0.88]
    cooldown_hours = [4, 8, 12]

    print(f"{'Lookback':>8}  {'Threshold':>9}  {'Cooldown':>8}  {'n':>5}  {'WR':>6}  {'P&L':>8}  {'vs base':>8}  {'skipped':>7}")
    print("-" * 75)

    best_pnl   = pnl0
    best_params = None

    for lb, thr, cool in product(lookbacks, thresholds, cooldown_hours):
        n, w, pnl, skipped = simulate(trades, lb, thr, cool)
        wr = w / n * 100 if n else 0
        diff = pnl - pnl0
        marker = " ◀" if pnl > best_pnl else ""
        if pnl > best_pnl:
            best_pnl = pnl
            best_params = (lb, thr, cool)
        print(f"{lb:>8}  {thr*100:>8.0f}%  {cool:>7}h  {n:>5}  {wr:>5.1f}%  ${pnl:>+7.0f}  ${diff:>+7.0f}  {skipped:>6}{marker}")

    print(f"\nBest: lookback={best_params[0]}, threshold={best_params[1]*100:.0f}%, cooldown={best_params[2]}h  → ${best_pnl:+.0f}")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
