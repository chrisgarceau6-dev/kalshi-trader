#!/usr/bin/env python3
"""Step 4b: score MAKER entries. The half of the search space scan.py cannot see.

WHAT IS NEW
-----------
scan.py's residual is `(won - ask) - fee(ask)`: a taker, paying up, paying the fee.
Every cell it has ever printed is that trade. It is why BOTH sides of every market
scan negative -- `yes_ask + no_ask ~ 102c`, so whichever side you take you pay ~2c of
spread plus the fee. A resting order pays neither. CLAUDE.md 2026-08-25 records 998
maker contracts filled at $0.00 against 0.5493c/ct taker, and calls it the single
largest unexploited fact in the search. This measures it.

THE MECHANISM, STATED SO IT CAN BE FALSIFIED
--------------------------------------------
Kalshi's taker fee is `0.07*P*(1-P)` -- 1.750c at 50c, 0.515c at 92c. A taker must
therefore be right by more than (half-spread + fee) before crossing is worth it, which
is a 2-3c moat at mid prices. That moat suppresses INFORMED taking, and informed
taking is exactly what makes resting orders lose money. So the claim is not "free
money from the spread"; it is "adverse selection is unusually cheap here because the
exchange taxes the people who would inflict it."

If that is right the edge should be LARGEST near 50c (where the fee moat is widest)
and SMALLEST near the extremes -- the opposite shape to late-certainty. If instead it
is largest at 90-93c, this is late-certainty wearing a hat and should be discarded.

THE FILL MODEL
--------------
Candles carry OHLC for `price` (actual trade prints), `yes_bid` and `yes_ask`. That
last fact is what makes this measurable at all: a resting bid at B was filled during a
minute iff someone traded at or below B.

  JOIN    B = bid_c(t)      -- rest at the existing best bid, at the BACK of that
                               queue. Only filled once the level is CLEARED:
                               px_l(t+1) < B or bid_l(t+1) < B. Deliberately
                               pessimistic: it counts a fill only when price is on its
                               way DOWN through you, i.e. it keeps the adversely
                               selected fills and throws away the benign ones.
  IMPROVE B = bid_c(t)+1    -- step in front of the queue (needs spread >= 2c so the
                               order does not cross). Alone at the front, so any sale
                               at or below B hits you: px_l(t+1) <= B.

IMPROVE is the strategy you would actually run; JOIN is the lower bound. If the edge
survives JOIN it is not a queue-position artifact.

Fills are held to SETTLEMENT, never closed out. That is not laziness -- selling to
close pays a taker fee and would hand back the entire advantage. Net per contract is
`100*won - B`, with no fee term at all.

WHAT MUST BE TRUE FOR THE OUTPUT TO MEAN ANYTHING
-------------------------------------------------
`--validate` re-derives the TAKER numbers from this same frame. They have to match
scan.py (the live 90-93c/prior>=90 band at ~+1.7c). If they do not, the OHLC pull is
wrong and nothing below is worth reading.

    python3 research/search2/maker_scan.py --validate
    python3 research/search2/maker_scan.py --by price
    python3 research/search2/maker_scan.py --by series price --holdout 0.3
"""
import argparse, glob, os, sys
from collections import defaultdict
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data_ohlc")
FEE_RATE = 0.07
MIN_CLUSTERS = 30


def fee_cents(p):
    return FEE_RATE * (p / 100.0) * (1 - p / 100.0) * 100


def load(series=None, path=DATA):
    files = sorted(glob.glob(os.path.join(path, "*.csv.gz")))
    if series:
        files = [f for f in files if os.path.basename(f)[:-7] in series]
    if not files:
        raise SystemExit(f"no data in {path} -- run pull_ohlc.py")
    d = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    d["won"] = d.won.astype(str).str.lower().isin(["true", "1"])

    # The first candle of a market has an empty book (bid_o 0.0 / ask_o 99.9 seen live)
    # and its "quote" is not a price anyone could have traded on.
    ok = (d.bid_c.between(1, 98)) & (d.ask_c.between(2, 99)) & (d.ask_c > d.bid_c)
    d = d[ok].copy()

    d = d.sort_values(["ticker", "side", "ts"])
    g = d.groupby(["ticker", "side"], sort=False)
    # The NEXT minute is where a resting order placed now would fill.
    for c in ["px_l", "px_h", "bid_l", "bid_c", "ask_c", "volume"]:
        d[f"n_{c}"] = g[c].shift(-1)
    # Path features, same definition as scan.py so the two are comparable.
    d["p1"] = g["ask_c"].shift(1)
    d["p2"] = g["ask_c"].shift(2)
    d["p3"] = g["ask_c"].shift(3)
    d["prior_min"] = d[["p1", "p2"]].min(axis=1)
    d["trend"] = d.ask_c - d.p3
    d = d[d.n_px_l.notna() | d.n_bid_l.notna()].copy()
    d["spread"] = d.ask_c - d.bid_c
    return d


def entries(d, mode):
    """Return (price, filled) for one entry style."""
    if mode == "taker":
        return d.ask_c, pd.Series(True, index=d.index)
    if mode == "join":
        B = d.bid_c
        # cleared through: a print strictly below, or the level itself gone
        filled = (d.n_px_l < B - 1e-9) | (d.n_bid_l < B - 1e-9)
        return B, filled.fillna(False)
    if mode == "improve":
        B = d.bid_c + 1
        ok = d.spread >= 2                      # else the order would cross the ask
        filled = ok & (d.n_px_l <= B + 1e-9)
        return B, filled.fillna(False)
    raise ValueError(mode)


def net_cents(d, B, mode):
    pnl = 100.0 * d.won.astype(float) - B
    if mode == "taker":
        pnl = pnl - B.map(fee_cents)
    return pnl


def bucket_price(a):
    e = [0, 10, 25, 35, 45, 55, 65, 75, 85, 90, 94, 97, 101]
    lab = ["01-09", "10-24", "25-34", "35-44", "45-54", "55-64", "65-74",
           "75-84", "85-89", "90-93", "94-96", "97-99"]
    return pd.cut(a, bins=e, labels=lab, right=False)


def bucket_secs(s):
    return pd.cut(s, bins=[-1, 300, 900, 1800, 3600, 10**9],
                  labels=["0-5m", "5-15m", "15-30m", "30-60m", "60m+"])


def add_dims(d, B):
    d = d.copy()
    d["price"] = bucket_price(B)
    d["secs"] = bucket_secs(d.secs_left)
    d["spr"] = pd.cut(d.spread, bins=[0, 1.01, 3.01, 1e9],
                      labels=["tight<=1c", "mid 2-3c", "wide>3c"])
    d["priors"] = pd.cut(d.prior_min, bins=[-1, 40, 60, 75, 90, 101],
                         labels=["prior<40", "prior40-59", "prior60-74",
                                 "prior75-89", "prior>=90"])
    d["trendb"] = pd.cut(d.trend, bins=[-1e9, -3, -1, 1, 3, 1e9],
                         labels=["falling>3c", "falling1-3c", "flat",
                                 "rising1-3c", "rising>3c"])
    return d


def boot(tot, cnt, iters=1500, seed=11):
    """Cluster bootstrap over close timestamps. tot/cnt are per-cluster sums."""
    n = len(tot)
    if n < MIN_CLUSTERS:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(iters, n))
    num = tot[idx].sum(axis=1)
    den = cnt[idx].sum(axis=1)
    m = den > 0
    if not m.any():
        return np.nan, np.nan
    r = np.sort(num[m] / den[m])
    return r[int(0.025 * len(r))], r[int(0.975 * len(r))]


def cell_stats(sub):
    g = sub.groupby("close_ts", observed=True)["net"].agg(["sum", "count"])
    lo, hi = boot(g["sum"].to_numpy(float), g["count"].to_numpy(float))
    return dict(n=len(sub), clusters=len(g), net=sub.net.mean(),
                avg_px=sub.B.mean(), wr=sub.won.mean(), lo=lo, hi=hi)


def run(d, mode, dims, min_n, min_clusters, top, quiet=False):
    B, filled = entries(d, mode)
    f = d[filled].copy()
    f["B"] = B[filled]
    f["net"] = net_cents(f, f.B, mode)
    f = add_dims(f, f.B)
    fill_rate = float(filled.mean())

    rows = []
    for key, sub in f.groupby(dims, observed=True):
        if len(sub) < min_n:
            continue
        st = cell_stats(sub)
        if st["clusters"] < min_clusters:
            continue
        st["key"] = key if isinstance(key, tuple) else (key,)
        rows.append(st)
    rows.sort(key=lambda r: -r["net"])
    if not quiet:
        print(f"\n=== {mode.upper()}  by {' x '.join(dims)}   "
              f"fill rate {fill_rate:.1%}  ({len(f):,} fills of {len(d):,} chances)")
        print(f"  {'cell':<40}{'n':>9}{'clus':>7}{'avg px':>8}{'WR':>8}"
              f"{'NET c/ct':>10}   95% CI")
        print("  " + "-" * 98)
        show = rows[:top] + ([None] + rows[-3:] if len(rows) > top + 3 else [])
        for r in show:
            if r is None:
                print("  " + "." * 98)
                continue
            ci = "—" if np.isnan(r["lo"]) else f"[{r['lo']:+.2f}, {r['hi']:+.2f}]"
            mark = ""
            if not np.isnan(r["lo"]):
                mark = "  <-- CI>0" if r["lo"] > 0 else ("  <-- NEG" if r["hi"] < 0 else "")
            print(f"  {' / '.join(map(str, r['key']))[:40]:<40}{r['n']:>9,}"
                  f"{r['clusters']:>7,}{r['avg_px']:>7.1f}c{r['wr']:>7.1%}"
                  f"{r['net']:>+10.2f}   {ci}{mark}")
    return rows, fill_rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", nargs="+", default=None)
    ap.add_argument("--series", nargs="+")
    ap.add_argument("--mode", nargs="+", default=["join", "improve"],
                    choices=["join", "improve", "taker"])
    ap.add_argument("--min-n", type=int, default=400)
    ap.add_argument("--min-clusters", type=int, default=MIN_CLUSTERS)
    ap.add_argument("--top", type=int, default=16)
    ap.add_argument("--holdout", type=float, default=0.0)
    ap.add_argument("--insample", type=float, default=0.0)
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()

    d = load(a.series)
    if a.holdout > 0 or a.insample > 0:
        cl = np.sort(d.close_ts.unique())
        frac = a.holdout or a.insample
        cut = cl[int(len(cl) * (1 - frac))]
        d = d[d.close_ts >= cut] if a.holdout else d[d.close_ts < cut]
        tag = "HOLDOUT most recent" if a.holdout else "IN-SAMPLE earliest"
        print(f"{tag} {frac:.0%} of close clusters")
    print(f"{len(d):,} quote-minutes | {d.close_ts.nunique():,} close clusters | "
          f"{d.series.nunique()} series | {d.ticker.nunique():,} markets")

    if a.validate:
        print("\nVALIDATION -- taker residuals from THIS frame must match scan.py.")
        print("scan.py reports the live 90-93c band at -1.69c without path features "
              "and\n+1.71c with prior>=90. If these disagree the OHLC pull is wrong.")
        run(d, "taker", ["price"], a.min_n, a.min_clusters, 20)
        run(d, "taker", ["price", "priors"], a.min_n, a.min_clusters, 24)
        return 0

    plans = [a.by] if a.by else [["price"], ["series"], ["secs"], ["spr"],
                                 ["price", "spr"], ["price", "secs"],
                                 ["series", "price"], ["price", "priors"]]
    for mode in a.mode:
        for dims in plans:
            run(d, mode, dims, a.min_n, a.min_clusters, a.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
