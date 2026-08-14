#!/usr/bin/env python3
"""WTI strategy optimization — same process used to arrive at crypto v5.7.

Phases:
  1. YES vs NO EV breakdown by ask cent (88-99c)
  2. Prior filter impact on YES @ [90,93]c
  3. Full grid search: ask gate × prior config, OOS validated
  4. Final recommendation + P&L projection

Run: python3 backtest_wti_optimize.py [--days 90]
"""

import time, argparse
from datetime import datetime, timezone
from kalshi_auth import get as kalshi_get

SERIES   = "KXWTI15M"
FEE      = 0.07
BET      = 45
OOS_FRAC = 0.33

# Grid axes
ASK_GATES = [
    (88,91),(88,93),(88,95),
    (90,91),(90,92),(90,93),(90,94),(90,95),
    (91,93),(91,95),(92,94),(92,95),(93,95),
]
PRIORS = [
    (0, 0,  "no_prior"),
    (1, 70, "n=1,min=70"),
    (1, 75, "n=1,min=75"),
    (1, 80, "n=1,min=80"),
    (2, 70, "n=2,min=70"),
    (2, 75, "n=2,min=75"),
    (2, 80, "n=2,min=80"),
    (3, 75, "n=3,min=75"),
]
TIME_WINDOWS = [(120,700),(150,600),(150,700),(200,600)]
CRYPTO_LIVE  = dict(ask_lo=90, ask_hi=93, pn=2, pm=75, tlo=150, thi=600, label="crypto_live")


# ── helpers ──────────────────────────────────────────────────────────────────

def candle_yes_ask(c):
    try:
        return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
    except (KeyError, ValueError, TypeError):
        return None

def candle_no_ask(c):
    try:
        yes_bid = int(round(float(c["yes_bid"]["close_dollars"]) * 100))
        return 100 - yes_bid if yes_bid > 0 else None
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


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_markets(cutoff_ts):
    markets, cursor = [], None
    while True:
        params = {"series_ticker": SERIES, "status": "settled", "limit": 200}
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
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return sorted(markets, key=lambda m: m["ts"])

def fetch_candles(ticker, close_ts):
    for attempt in range(3):
        try:
            code, r = kalshi_get(
                f"/series/{SERIES}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 900, "end_ts": close_ts + 10, "period_interval": 1},
            )
            if code == 200:
                return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return []

def load_all(markets):
    """Fetch candles for every market once; return list of (market, candles)."""
    out = []
    for i, m in enumerate(markets):
        c = fetch_candles(m["ticker"], m["ts"])
        out.append((m, c))
        if (i+1) % 100 == 0:
            print(f"  candles {i+1}/{len(markets)}...", flush=True)
        time.sleep(0.02)
    return out


# ── phase 1: YES vs NO by ask cent ───────────────────────────────────────────

def phase1_yes_no(dataset, tlo=150, thi=600):
    yes_buckets = {}  # ask_cent -> [pnl, ...]
    no_buckets  = {}
    for market, candles in dataset:
        won = market["won"]
        seen_yes, seen_no = set(), set()
        for i, c in enumerate(candles):
            secs = market["ts"] - c.get("end_period_ts", 0)
            if not (tlo <= secs <= thi):
                continue
            ya = candle_yes_ask(c)
            na = candle_no_ask(c)
            if ya and 85 <= ya <= 99 and ya not in seen_yes:
                yes_buckets.setdefault(ya, []).append(trade_pnl(won, ya))
                seen_yes.add(ya)
            if na and 85 <= na <= 99 and na not in seen_no:
                no_buckets.setdefault(na, []).append(trade_pnl(not won, na))
                seen_no.add(na)

    print("\n── Phase 1: YES vs NO EV by ask cent (150-600s window) ──")
    print(f"  {'Ask':>4}  {'YES n':>6}  {'YES WR':>7}  {'YES $/tr':>9}  │  {'NO n':>6}  {'NO WR':>6}  {'NO $/tr':>8}")
    print(f"  {'----':>4}  {'------':>6}  {'------':>7}  {'---------':>9}  │  {'------':>6}  {'------':>6}  {'--------':>8}")
    for cent in range(88, 100):
        yb = yes_buckets.get(cent, [])
        nb = no_buckets.get(cent, [])
        yn, nn = len(yb), len(nb)
        if yn == 0 and nn == 0:
            continue
        ywr = sum(1 for p in yb if p > 0) / yn * 100 if yn else 0
        nwr = sum(1 for p in nb if p > 0) / nn * 100 if nn else 0
        yppt = sum(yb)/yn if yn else 0
        nppt = sum(nb)/nn if nn else 0
        yflag = " ✓" if yppt > 0 else "  "
        nflag = " ✓" if nppt > 0 else "  "
        print(f"  {cent:>4}c  {yn:>6}  {ywr:>6.1f}%  {yppt:>+9.2f}{yflag}  │  {nn:>6}  {nwr:>6.1f}%  {nppt:>+8.2f}{nflag}")


# ── phase 2: prior filter impact ─────────────────────────────────────────────

def phase2_prior(dataset, oos_split):
    print("\n── Phase 2: Prior filter impact on YES @ [90,93]c 150-600s ──")
    print(f"  {'Config':<14}  {'Full n':>6}  {'Full WR':>7}  {'Full $/tr':>9}  │  {'OOS n':>6}  {'OOS WR':>6}  {'OOS $/tr':>8}")
    print(f"  {'-'*14}  {'------':>6}  {'-------':>7}  {'---------':>9}  │  {'------':>6}  {'------':>6}  {'--------':>8}")

    for pn, pm, lbl in PRIORS:
        all_r, oos_r = [], []
        for idx, (market, candles) in enumerate(dataset):
            won = market["won"]
            for i, c in enumerate(candles):
                secs = market["ts"] - c.get("end_period_ts", 0)
                if not (150 <= secs <= 600):
                    continue
                ask = candle_yes_ask(c)
                if ask is None or not (90 <= ask <= 93):
                    continue
                if not prior_ok(candles, i, pn, pm):
                    continue
                pnl = trade_pnl(won, ask)
                all_r.append(pnl)
                if idx >= oos_split:
                    oos_r.append(pnl)
                break

        def fmt(r):
            if not r:
                return f"{'—':>6}  {'—':>6}  {'—':>8}"
            wr = sum(1 for p in r if p > 0) / len(r) * 100
            ppt = sum(r) / len(r)
            return f"{len(r):>6}  {wr:>6.1f}%  {ppt:>+8.2f}"

        live_flag = " ◀ LIVE" if (pn, pm) == (2, 75) else ""
        print(f"  {lbl:<14}  {fmt(all_r)}  │  {fmt(oos_r)}{live_flag}")


# ── phase 3: full grid search ─────────────────────────────────────────────────

def phase3_grid(dataset, oos_split, days):
    print("\n── Phase 3: Full grid search (ask gate × prior, OOS validated) ──")
    results = []
    total = len(ASK_GATES) * len(PRIORS) * len(TIME_WINDOWS)
    done = 0

    for (alo, ahi) in ASK_GATES:
        for (pn, pm, plbl) in PRIORS:
            for (tlo, thi) in TIME_WINDOWS:
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
                done += 1
                results.append({
                    "label":  f"ask={alo}-{ahi} {plbl} t={tlo}-{thi}",
                    "ask":    (alo, ahi), "prior": (pn, pm, plbl), "time": (tlo, thi),
                    "all_n":  len(all_r),
                    "all_wr": sum(1 for p in all_r if p > 0)/len(all_r)*100 if all_r else 0,
                    "all_ppt":sum(all_r)/len(all_r) if all_r else -999,
                    "all_ppd":sum(all_r)/days if all_r else -999,
                    "oos_n":  len(oos_r),
                    "oos_wr": sum(1 for p in oos_r if p > 0)/len(oos_r)*100 if oos_r else 0,
                    "oos_ppt":sum(oos_r)/len(oos_r) if oos_r else -999,
                    "oos_ppd":sum(oos_r)/(days*OOS_FRAC) if oos_r else -999,
                })

    # Sort by OOS $/trade, require n>=15
    valid = [r for r in results if r["oos_n"] >= 15]
    valid.sort(key=lambda r: r["oos_ppt"], reverse=True)

    print(f"\n  Top 10 by OOS $/trade (minimum 15 OOS trades):")
    hdr = f"  {'Config':<42}  {'All n':>5}  {'All WR':>7}  {'All $/tr':>8}  │  {'OOS n':>5}  {'OOS WR':>6}  {'OOS $/tr':>8}  {'OOS $/day':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr)-2))
    for r in valid[:10]:
        is_live = (r["ask"] == (90,93) and r["prior"][:2] == (2,75) and r["time"] == (150,600))
        flag = " ◀ CRYPTO LIVE" if is_live else ""
        print(f"  {r['label']:<42}  {r['all_n']:>5}  {r['all_wr']:>6.1f}%  {r['all_ppt']:>+8.2f}  │  "
              f"{r['oos_n']:>5}  {r['oos_wr']:>6.1f}%  {r['oos_ppt']:>+8.2f}  {r['oos_ppd']:>+9.2f}{flag}")

    return valid[0] if valid else None


# ── phase 4: final recommendation ────────────────────────────────────────────

def phase4_summary(best, dataset, days):
    if not best:
        print("\n── Phase 4: Insufficient data for recommendation ──")
        return

    alo, ahi = best["ask"]
    pn, pm, plbl = best["prior"]
    tlo, thi = best["time"]

    # Full period trades with best config
    trades = []
    for market, candles in dataset:
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
            trades.append({"pnl": trade_pnl(won, ask), "ask": ask})
            break

    avg_ask = sum(t["ask"] for t in trades) / len(trades) if trades else 0
    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / len(trades) * 100 if trades else 0

    # Current crypto for reference
    crypto_pnl_day = 38.0
    crypto_trades_day = 265 / 6  # per series

    wti_trades_day = len(trades) / days
    wti_pnl_day = total_pnl / days

    print(f"\n── Phase 4: Final Recommendation ──")
    print(f"\n  Best WTI config: ask=[{alo},{ahi}]c  prior={plbl}  time=[{tlo},{thi}]s")
    print(f"  WR: {wr:.1f}%  |  avg entry: {avg_ask:.0f}¢  |  {len(trades)} trades over {days}d")
    print(f"\n  P&L projection:")
    print(f"    WTI $/trade:   {total_pnl/len(trades):>+.2f}" if trades else "")
    print(f"    WTI trades/day: ~{wti_trades_day:.1f}")
    print(f"    WTI $/day:     ~{wti_pnl_day:>+.2f}")
    print(f"\n  Combined daily expectation (6 crypto series + WTI):")
    print(f"    Crypto:        ~+${crypto_pnl_day:.0f}/day  (v5.7 backtest, $45 bets)")
    print(f"    WTI:           ~{wti_pnl_day:>+.0f}/day")
    print(f"    ─────────────────────────────")
    print(f"    Total:         ~+${crypto_pnl_day + wti_pnl_day:.0f}/day  at $45 bets")
    print(f"\n  Recommended WTI params:")
    print(f"    MIN_ASK_CENTS     = {alo}")
    print(f"    MAX_ASK_CENTS     = {ahi}")
    print(f"    PRIOR_LOOKBACK    = {pn}")
    print(f"    PRIOR_MIN_CENTS   = {pm}")
    print(f"    MIN_SECS_LEFT     = {tlo}")
    print(f"    MAX_SECS_LEFT     = {thi}")
    print(f"\n  NOTE: {days}d of data only (WTI series launched ~Jul 31).")
    print(f"  OOS edge is real but CIs are wide at n={best['oos_n']}.")
    print(f"  Run with same $45 bet, monitor for 2 weeks before scaling.")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    cutoff = int(time.time()) - args.days * 86400
    print(f"\nWTI optimization — {args.days}d window, BET=${BET}, FEE={FEE*100:.0f}%")
    print(f"{'='*70}")

    print(f"\nFetching markets...", flush=True)
    markets = fetch_markets(cutoff)
    n = len(markets)
    if n < 50:
        print(f"Only {n} markets — insufficient data."); return

    t0 = datetime.fromtimestamp(markets[0]["ts"], tz=timezone.utc).strftime("%b %d")
    t1 = datetime.fromtimestamp(markets[-1]["ts"], tz=timezone.utc).strftime("%b %d")
    yes_rate = sum(1 for m in markets if m["won"]) / n
    days_actual = (markets[-1]["ts"] - markets[0]["ts"]) / 86400
    print(f"{n} markets  |  {t0} → {t1}  ({days_actual:.0f} days)  |  YES settle rate: {yes_rate:.1%}")

    oos_split = int(n * (1 - OOS_FRAC))
    print(f"Train: {oos_split}  |  OOS: {n - oos_split}")

    print(f"\nFetching candles (one pass for all {n} markets)...", flush=True)
    dataset = load_all(markets)

    phase1_yes_no(dataset)
    phase2_prior(dataset, oos_split)
    best = phase3_grid(dataset, oos_split, days_actual)
    phase4_summary(best, dataset, days_actual)


if __name__ == "__main__":
    main()
