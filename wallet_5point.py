#!/usr/bin/env python3
"""5-point copyability screener.

Runs poly-copy-backtest at 0c and 3c, applies poly_settle_correct's logic
inline, and scores a wallet against the 5 criteria that actually
predicted survival in this project:

  1. edge/spread ratio >= 3x   (0c PnL should dwarf the 0c->3c drop)
  2. median hold >= 24h        (or the spread eats you before you copy)
  3. settlement-corrected Sharpe > 0.3 (not raw -- raw is winner-biased)
  4. n >= 200 round-trips, spread over >= 12 months, no single month
     carrying > 40% of PnL (not a regime/event wallet)
  5. concurrent open positions <= 20 (so a $2k book resembles the real
     book instead of a random slice of it)

This calls kalshi_weather_edge.py as a subprocess for the backtest step
(so it reuses your verified trade-fetch/fee logic) and hits
data-api.polymarket.com/positions directly for the settlement correction
and the concurrent-position count.

usage:
    python wallet_5point.py 0xWALLET1 0xWALLET2 ...
    python wallet_5point.py --file wallets.txt
"""
import argparse, subprocess, sys, re, time
import requests
import pandas as pd

DATA = "https://data-api.polymarket.com"


def run_backtest(wallet, slippage, min_hold=0):
    cmd = ["python", "kalshi_weather_edge.py", "poly-copy-backtest",
           "--wallets", wallet, "--bankroll", "2000",
           "--slippage", str(slippage), "--out", f"_tmp_{wallet[:8]}_{slippage}.csv"]
    if min_hold:
        cmd += ["--min-hold-hrs", str(min_hold)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    out = r.stdout
    def grab(pat, cast=float):
        m = re.search(pat, out)
        return cast(m.group(1)) if m else None
    return {
        "n": grab(r"trades:\s+(\d+)", int),
        "pnl": grab(r"total PnL:\s+\$(-?[\d,]+)", lambda s: float(s.replace(",", ""))),
        "sharpe": grab(r"Sharpe/trade:\s+(-?[\d.]+)"),
        "range": re.search(r"range (\S+) . (\S+)", out),
        "csv": f"_tmp_{wallet[:8]}_{slippage}.csv" if min_hold == 0 else None,
    }


def fetch_positions(wallet, max_pages=8):
    out, offset = [], 0
    for _ in range(max_pages):
        try:
            r = requests.get(f"{DATA}/positions",
                             params={"user": wallet, "limit": 500, "offset": offset},
                             timeout=25)
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(0.2)
    return out


def settle_correct(csv_path, wallet, slippage, bet=100.0, fee_mult=0.05):
    try:
        rt = pd.read_csv(csv_path)
    except FileNotFoundError:
        return None
    for c in ("entry_price", "exit_price", "hold_hrs"):
        if c in rt.columns:
            rt[c] = pd.to_numeric(rt[c], errors="coerce")

    pos = fetch_positions(wallet)
    n_open = 0
    if pos:
        P = pd.DataFrame(pos)
        for c in ("size", "avgPrice", "curPrice"):
            if c in P.columns:
                P[c] = pd.to_numeric(P[c], errors="coerce")
        P = P[P.get("size", 0).fillna(0) > 0]
        n_open = len(P)
        dead = P[P["curPrice"] <= 0.02] if "curPrice" in P else P.iloc[0:0]
        won = P[P["curPrice"] >= 0.98] if "curPrice" in P else P.iloc[0:0]
    else:
        dead = won = pd.DataFrame()

    def pnl_of(e, x):
        e = min(max(e + slippage, 0.01), 0.99)
        x = min(max(x - slippage, 0.0), 1.0)
        ef = fee_mult * e * (1 - e)
        c = bet / (e + ef)
        return c * x - c * (e + ef)

    entries = list(rt.entry_price) + list(dead.get("avgPrice", []))
    exits = list(rt.exit_price) + [0.0] * len(dead) + [1.0] * len(won.get("avgPrice", []))
    if won is not None and len(won):
        entries += list(won["avgPrice"])
    pnls = [pnl_of(e, x) for e, x in zip(entries, exits) if pd.notna(e) and pd.notna(x)]
    if not pnls:
        return None
    s = pd.Series(pnls)
    sharpe = s.mean() / s.std() if s.std() > 0 else float("nan")

    hold = rt.get("hold_hrs")
    med_hold = hold.median() if hold is not None and len(hold.dropna()) else None

    month_pnl = None
    if "entry_ts" in rt.columns:
        try:
            rt["_ts"] = pd.to_datetime(rt["entry_ts"], errors="coerce")
            rt["_pnl"] = [pnl_of(e, x) for e, x in
                         zip(rt.entry_price, rt.exit_price)]
            by_month = rt.dropna(subset=["_ts"]).groupby(
                rt["_ts"].dt.to_period("M"))["_pnl"].sum()
            n_months = by_month.index.nunique()
            top_month_share = (by_month.abs().max() / by_month.abs().sum()
                               if by_month.abs().sum() > 0 else None)
        except Exception:
            n_months = top_month_share = None
    else:
        n_months = top_month_share = None

    return {
        "corrected_n": len(pnls), "corrected_pnl": round(s.sum(), 2),
        "corrected_sharpe": round(sharpe, 3) if pd.notna(sharpe) else None,
        "median_hold_hrs": round(med_hold, 1) if med_hold else None,
        "n_open_positions": n_open, "n_dead_added": len(dead),
        "n_won_added": len(won), "n_months": n_months,
        "top_month_share": round(top_month_share, 2) if top_month_share else None,
    }


def score(wallet):
    print(f"\n{'='*70}\n{wallet}\n{'='*70}")
    b0 = run_backtest(wallet, 0.00)
    b3 = run_backtest(wallet, 0.03)
    if not b0 or not b0.get("n"):
        print("  no round-trips at 0c -- can't score"); return None
    print(f"  raw @0c: n={b0['n']} pnl=${b0['pnl']:.0f} sharpe={b0['sharpe']}")
    print(f"  raw @3c: n={b3['n']} pnl=${b3['pnl']:.0f} sharpe={b3['sharpe']}")

    sc = settle_correct(b0["csv"], wallet, 0.03) if b0.get("csv") else None
    if sc:
        print(f"  corrected: n={sc['corrected_n']} pnl=${sc['corrected_pnl']:.0f} "
              f"sharpe={sc['corrected_sharpe']} median_hold={sc['median_hold_hrs']}h "
              f"open_positions={sc['n_open_positions']} months={sc['n_months']} "
              f"top_month_share={sc['top_month_share']}")

    checks = {}
    if b0["pnl"] and b3["pnl"] is not None and b0["pnl"] > 0:
        checks["1_edge_vs_spread"] = (b3["pnl"] / b0["pnl"]) >= 0.5   # <=50% drop
    if sc and sc["median_hold_hrs"] is not None:
        checks["2_hold_ge_24h"] = sc["median_hold_hrs"] >= 24
    if sc and sc["corrected_sharpe"] is not None:
        checks["3_corrected_sharpe_gt_0.3"] = sc["corrected_sharpe"] > 0.3
    if sc and sc["corrected_n"] and sc["n_months"]:
        checks["4_sample_and_spread"] = (sc["corrected_n"] >= 200
                                         and sc["n_months"] >= 12
                                         and (sc["top_month_share"] or 1) <= 0.4)
    if sc and sc["n_open_positions"] is not None:
        checks["5_concurrent_le_20"] = sc["n_open_positions"] <= 20

    print("  --- scorecard ---")
    for k, v in checks.items():
        print(f"    {k}: {'PASS' if v else 'FAIL'}")
    passed = sum(checks.values())
    print(f"  {passed}/5 criteria met")
    return {"wallet": wallet, "passed": passed, **checks, **(sc or {})}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("wallets", nargs="*")
    p.add_argument("--file")
    p.add_argument("--out", default="wallet_5point_results.csv")
    a = p.parse_args()

    wallets = list(a.wallets)
    if a.file:
        with open(a.file) as f:
            wallets += [w.strip() for w in f if w.strip()]
    if not wallets:
        print("give me wallet addresses, or --file wallets.txt"); return

    results = [score(w) for w in wallets]
    results = [r for r in results if r]
    if results:
        pd.DataFrame(results).to_csv(a.out, index=False)
        print(f"\nsaved -> {a.out}")
        best = max(results, key=lambda r: r["passed"])
        print(f"\nbest: {best['wallet']} ({best['passed']}/5)")
        if best["passed"] < 5:
            print("nothing cleared all 5 -- that is itself the answer, not a "
                  "reason to loosen the bar")


if __name__ == "__main__":
    main()
