#!/usr/bin/env python3
"""Ablation study: start from core edge and add filters one by one.

Saves all qualifying candle candidates (with spot prices) to
backtest_ablation_raw.csv on first run. Subsequent runs re-use the cached
CSV — new filter tests run in seconds, not hours.

Ablation sequence:
  0. Broad:  YES, 90-93c, 100-800s  (no timing restriction)
  1. Core:   YES, 90-93c, 150-600s  (+ timing filter)
  2. + Prior filter (2-candle >= 75c)
  3. + UTC 17 blackout
  4. + UTC 15+17 blackout (current live)
  5. + Near-strike filter (10 bps)
  6. + H4 momentum filter (5 bps adverse)
  7. + MAX_CONCURRENT = 2

Key design: ALL qualifying candles per market are saved (not just the first).
The ablation picks the first candle that passes ALL active filters — matching
how the live bot would reconsider a market on the next polling cycle.

Run:           python3 backtest_ablation.py [--days 60]
Force refetch: python3 backtest_ablation.py --refetch
"""

import csv, json, os, time, urllib.request, argparse
from collections import defaultdict
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
COINBASE_PAIR = {
    "KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD", "KXBNB15M": "BNB-USD", "KXXRP15M": "XRP-USD",
}
H4_BPS         = 5
NEAR_BPS       = 10
FEE            = 0.07
BET            = 45
RAW_CSV        = "backtest_ablation_raw.csv"
SPOT_CSV       = "backtest_ablation_spot.csv"
CB_GRANULARITY = 60
CB_CHUNK       = 300


# ── Coinbase spot pre-fetch ────────────────────────────────────────────────

def fetch_coinbase_chunk(pair, start_ts, end_ts):
    url = (f"https://api.exchange.coinbase.com/products/{pair}/candles"
           f"?granularity={CB_GRANULARITY}&start={start_ts}&end={end_ts}")
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-ablation/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # [[timestamp, low, high, open, close, volume]]; timestamp = candle START
        return {int(row[0]): float(row[4]) for row in data if len(row) >= 5}
    except Exception:
        return {}


def prefetch_coinbase(cutoff_ts, now_ts):
    if os.path.exists(SPOT_CSV):
        print(f"Loading cached spot data from {SPOT_CSV}...")
        cache = {}
        with open(SPOT_CSV) as f:
            for row in csv.DictReader(f):
                p = row["pair"]
                if p not in cache:
                    cache[p] = {}
                cache[p][int(row["ts"])] = float(row["close"])
        total = sum(len(v) for v in cache.values())
        print(f"  Loaded {total:,} spot candles for {len(cache)} pairs")
        return cache

    print("Pre-fetching Coinbase 1-min spot data (~15 min)...")
    cache = {}
    all_rows = []
    for pair in COINBASE_PAIR.values():
        cache[pair] = {}
        chunks = 0
        ts = (cutoff_ts // 60) * 60  # align to minute
        while ts < now_ts:
            chunk_end = min(ts + CB_CHUNK * CB_GRANULARITY, now_ts)
            data = fetch_coinbase_chunk(pair, ts, chunk_end)
            cache[pair].update(data)
            all_rows.extend({"pair": pair, "ts": k, "close": v} for k, v in data.items())
            chunks += 1
            ts = chunk_end
            if chunks % 50 == 0:
                print(f"  {pair}: {chunks} chunks, {len(cache[pair]):,} candles", end="\r", flush=True)
            time.sleep(0.15)
        print(f"  {pair}: {len(cache[pair]):,} candles          ")

    with open(SPOT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pair", "ts", "close"])
        w.writeheader()
        w.writerows(all_rows)
    print(f"Saved {sum(len(v) for v in cache.values()):,} spot candles to {SPOT_CSV}")
    return cache


def get_spot_exact(spot_cache, series, minute_end_ts):
    """Return close for the completed 1-min bucket ending at minute_end_ts.
    Uses exact bucket start = floor(minute_end_ts/60)*60 - 60.
    Returns None (fail-open) if the exact bucket is missing."""
    pair = COINBASE_PAIR.get(series)
    if not pair:
        return None
    bucket_start = int(minute_end_ts // 60) * 60 - 60
    return spot_cache.get(pair, {}).get(bucket_start)


# ── Kalshi data fetch ──────────────────────────────────────────────────────

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


def collect_raw(days, spot_cache):
    """Collect ALL qualifying candles per market per side (not just the first).
    The ablation then picks the first candle that passes each filter set."""
    cutoff = int(time.time()) - days * 86400
    rows   = []
    stimes = []

    for series in SERIES_LIST:
        t0 = time.time()
        print(f"[{series}] fetching...", flush=True)
        markets = fetch_settled_markets(series, cutoff)
        print(f"[{series}] {len(markets)} markets", flush=True)

        for idx, m in enumerate(markets):
            if idx % 100 == 0:
                elapsed = time.time() - t0
                if stimes and idx > 0:
                    avg = sum(stimes) / len(stimes)
                    rem = (len(SERIES_LIST) - len(stimes) - 1) * avg + \
                          (len(markets) - idx) / max(idx, 1) * elapsed / 60
                    print(f"  {idx}/{len(markets)}  ~{rem:.0f}min left", end="\r", flush=True)
                else:
                    print(f"  {idx}/{len(markets)}", end="\r", flush=True)
            time.sleep(0.08)

            ticker       = m.get("ticker", "")
            result       = m.get("result", "")
            close_ts     = parse_close_ts(m)
            floor_strike = float(m.get("floor_strike") or 0)
            if not result or not close_ts:
                continue

            close_hour = datetime.fromtimestamp(close_ts, tz=timezone.utc).hour
            candles    = fetch_candles(ticker, series, close_ts)
            if not candles:
                continue

            # Save every qualifying candle (broad window), not just first.
            # Ablation picks first passing candle per (ticker, side).
            for i, c in enumerate(candles):
                end_ts    = c.get("end_period_ts", 0)
                secs_left = close_ts - end_ts
                if not (100 <= secs_left <= 800):
                    continue

                for side in ("yes", "no"):
                    ask = candle_price(c, side)
                    if ask is None or not (88 <= ask <= 96):
                        continue

                    def pcan(offset):
                        j = i - offset
                        return candle_price(candles[j], side) if j >= 0 else None

                    spot_now = get_spot_exact(spot_cache, series, end_ts)
                    spot_60s = get_spot_exact(spot_cache, series, end_ts - 60)

                    rows.append({
                        "series":       series,
                        "ticker":       ticker,
                        "close_ts":     close_ts,
                        "close_hour":   close_hour,
                        "candle_idx":   i,
                        "side":         side,
                        "ask":          ask,
                        "secs_left":    int(secs_left),
                        "won":          result == side,
                        "profit":       trade_pnl(result == side, ask),
                        "prior_1":      pcan(1) if pcan(1) is not None else "",
                        "prior_2":      pcan(2) if pcan(2) is not None else "",
                        "prior_3":      pcan(3) if pcan(3) is not None else "",
                        "floor_strike": floor_strike,
                        "spot_now":     spot_now if spot_now is not None else "",
                        "spot_60s":     spot_60s if spot_60s is not None else "",
                    })

        elapsed = time.time() - t0
        stimes.append(elapsed / 60)
        remaining = (len(SERIES_LIST) - len(stimes)) * (sum(stimes) / len(stimes))
        print(f"[{series}] done — {elapsed/60:.1f}min (~{remaining:.0f}min left)          ")

    return rows


def save_raw(rows, path):
    fields = ["series","ticker","close_ts","close_hour","candle_idx","side","ask",
              "secs_left","won","profit","prior_1","prior_2","prior_3",
              "floor_strike","spot_now","spot_60s"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Saved {len(rows)} raw rows to {path}")


def load_raw(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({
                "series":       r["series"],
                "ticker":       r["ticker"],
                "close_ts":     int(r["close_ts"]),
                "close_hour":   int(r["close_hour"]),
                "candle_idx":   int(r["candle_idx"]),
                "side":         r["side"],
                "ask":          int(r["ask"]),
                "secs_left":    int(r["secs_left"]),
                "won":          r["won"] == "True",
                "profit":       float(r["profit"]),
                "prior_1":      int(r["prior_1"]) if r["prior_1"] else None,
                "prior_2":      int(r["prior_2"]) if r["prior_2"] else None,
                "prior_3":      int(r["prior_3"]) if r["prior_3"] else None,
                "floor_strike": float(r["floor_strike"]) if r["floor_strike"] else 0.0,
                "spot_now":     float(r["spot_now"]) if r["spot_now"] else None,
                "spot_60s":     float(r["spot_60s"]) if r["spot_60s"] else None,
            })
    return rows


# ── Filter helpers ─────────────────────────────────────────────────────────

def prior_ok(r, n, min_c):
    if n == 0:
        return True
    for k in range(1, n + 1):
        p = r[f"prior_{k}"]
        if p is None or p < min_c:
            return False
    return True


def h4_ok(r):
    """True = pass. Fail open if spot data missing."""
    sn, s6 = r["spot_now"], r["spot_60s"]
    if not sn or not s6 or s6 == 0:
        return True
    try:
        sn, s6 = float(sn), float(s6)
    except (TypeError, ValueError):
        return True
    ret_60  = (sn - s6) / s6
    adverse = -ret_60 if r["side"] == "yes" else ret_60
    return adverse <= H4_BPS / 10000


def near_strike_ok(r):
    """True = pass. Fail open if strike or spot missing."""
    sn, sk = r["spot_now"], r["floor_strike"]
    if not sn or not sk:
        return True
    try:
        sn, sk = float(sn), float(sk)
    except (TypeError, ValueError):
        return True
    if sn == 0 or sk == 0:
        return True
    return abs(sn - sk) / sn >= NEAR_BPS / 10000


def simulate(all_rows, row_filter):
    """For each (ticker, side), find the first candle (highest secs_left)
    where row_filter passes. One trade per market per side."""
    groups = defaultdict(list)
    for r in all_rows:
        groups[(r["ticker"], r["side"])].append(r)

    result = []
    for candles in groups.values():
        candles.sort(key=lambda r: -r["secs_left"])  # earliest entry first
        for r in candles:
            if row_filter(r):
                result.append(r)
                break  # first passing candle only
    return result


def apply_concurrent_cap(rows, cap=2):
    """Within each 15-min expiry, keep at most `cap` trades
    by earliest entry (highest secs_left)."""
    groups = defaultdict(list)
    for r in rows:
        groups[r["close_ts"]].append(r)
    out = []
    for group in groups.values():
        group.sort(key=lambda r: -r["secs_left"])
        out.extend(group[:cap])
    return out


def summarize(rows):
    n    = len(rows)
    wins = sum(1 for r in rows if r["won"])
    pnl  = sum(r["profit"] for r in rows)
    return n, wins / n * 100 if n else 0, pnl


# ── Ablation ──────────────────────────────────────────────────────────────

def run_ablation(all_rows):
    steps = [
        ("0_broad",          "YES, 90-93c, 100-800s (no timing filter)",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 100<=r["secs_left"]<=800)),

        ("1_core",           "+ restrict to 150-600s",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600)),

        ("2_+prior",         "+ prior 2-candle >= 75c",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600
                       and prior_ok(r,2,75))),

        ("3_+bkout_UTC17",   "+ blackout UTC 17",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600
                       and prior_ok(r,2,75) and r["close_hour"]!=17)),

        ("4_+bkout_15+17",   "+ blackout UTC 15 (current live)",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600
                       and prior_ok(r,2,75) and r["close_hour"] not in {15,17})),

        ("5_+near_strike",   "+ near-strike filter (10 bps)",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600
                       and prior_ok(r,2,75) and r["close_hour"] not in {15,17}
                       and near_strike_ok(r))),

        ("6_+H4",            "+ H4 momentum filter (5 bps)",
         lambda rows: simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600
                       and prior_ok(r,2,75) and r["close_hour"] not in {15,17}
                       and near_strike_ok(r) and h4_ok(r))),

        ("7_+concurrent2",   "+ MAX_CONCURRENT = 2",
         lambda rows: apply_concurrent_cap(simulate(rows,
             lambda r: r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600
                       and prior_ok(r,2,75) and r["close_hour"] not in {15,17}
                       and near_strike_ok(r) and h4_ok(r)), cap=2)),
    ]

    print(f"\n{'Step':<22} {'Description':<40} {'n':>7}  {'WR':>7}  "
          f"{'$/trade':>8}  {'Net P&L':>10}  {'P&L delta':>10}")
    print("-" * 115)
    prev_pnl = None
    for name, desc, fn in steps:
        subset     = fn(all_rows)
        n, wr, pnl = summarize(subset)
        delta      = f"${pnl - prev_pnl:+.0f}" if prev_pnl is not None else "—"
        print(f"{name:<22} {desc:<40} {n:>7}  {wr:>6.1f}%  "
              f"${pnl/max(n,1):>+7.2f}/trade  ${pnl:>+9.0f}  {delta:>10}")
        prev_pnl = pnl

    # Spot filter coverage report
    yes_rows = [r for r in all_rows if r["side"]=="yes" and 90<=r["ask"]<=93 and 150<=r["secs_left"]<=600]
    missing_spot = sum(1 for r in yes_rows if r["spot_now"] is None)
    print(f"\nSpot coverage: {len(yes_rows)-missing_spot}/{len(yes_rows)} YES core rows "
          f"have Coinbase data ({missing_spot} fail-open)")
    print("Filters with missing spot data fail open (trade proceeds).")
    print("\nTo test a new filter: add a step to `steps` in run_ablation() and re-run.")
    print("No refetch needed — analysis uses cached backtest_ablation_raw.csv.")


# ── Parameter range analysis ───────────────────────────────────────────────

def run_parameter_analysis(all_rows):
    """Break down $/trade by ask price and time bucket to validate the
    chosen ranges. Results are hypothesis generation — not confirmation."""

    # Use the prior-filtered YES universe as the base (same as step 2)
    base = simulate(all_rows,
        lambda r: r["side"]=="yes" and 88<=r["ask"]<=96 and 100<=r["secs_left"]<=800
                  and prior_ok(r, 2, 75))

    print("\n── ASK PRICE BREAKDOWN (YES, 2-candle/75c prior, 100-800s) ──────────")
    print(f"  {'Ask':>5}  {'n':>6}  {'WR':>7}  {'$/trade':>8}  {'Net P&L':>10}")
    print(f"  {'—'*5}  {'—'*6}  {'—'*7}  {'—'*8}  {'—'*10}")
    for ask_c in range(88, 97):
        subset = [r for r in base if r["ask"] == ask_c]
        n, wr, pnl = summarize(subset)
        marker = " ◀ live range" if 90 <= ask_c <= 93 else ""
        print(f"  {ask_c:>5}c  {n:>6}  {wr:>6.1f}%  ${pnl/max(n,1):>+7.2f}/trade  ${pnl:>+9.0f}{marker}")

    print("\n── TIME WINDOW BREAKDOWN (YES, 90-93c, 2-candle/75c prior) ─────────")
    print(f"  {'Secs left':>12}  {'n':>6}  {'WR':>7}  {'$/trade':>8}  {'Net P&L':>10}")
    print(f"  {'—'*12}  {'—'*6}  {'—'*7}  {'—'*8}  {'—'*10}")
    buckets = [(100,150),(150,200),(200,300),(300,400),(400,500),(500,600),(600,700),(700,800)]
    for lo, hi in buckets:
        subset = simulate(all_rows,
            lambda r, lo=lo, hi=hi:
                r["side"]=="yes" and 90<=r["ask"]<=93 and lo<=r["secs_left"]<hi
                and prior_ok(r, 2, 75))
        n, wr, pnl = summarize(subset)
        marker = " ◀ live range" if lo >= 150 and hi <= 601 else ""
        print(f"  {lo:>5}-{hi:<5}s  {n:>6}  {wr:>6.1f}%  ${pnl/max(n,1):>+7.2f}/trade  ${pnl:>+9.0f}{marker}")

    print()
    print("NOTE: These breakdowns are post-hoc and have multiple-comparison risk.")
    print("      Use them to generate hypotheses, not to tune parameters.")
    print("      Any range change needs pre-registration + fresh OOS validation.")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",    type=int, default=60)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    now_ts = int(time.time())
    cutoff = now_ts - args.days * 86400

    if os.path.exists(RAW_CSV) and not args.refetch:
        print(f"Loading {RAW_CSV} (use --refetch to re-fetch from APIs)")
        rows = load_raw(RAW_CSV)
        print(f"Loaded {len(rows)} rows")
    else:
        spot_cache = prefetch_coinbase(cutoff, now_ts)
        rows       = collect_raw(args.days, spot_cache)
        save_raw(rows, RAW_CSV)

    run_ablation(rows)
    run_parameter_analysis(rows)


if __name__ == "__main__":
    main()
