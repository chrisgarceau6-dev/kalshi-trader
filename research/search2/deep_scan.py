#!/usr/bin/env python3
"""Step 7: re-run the TAKER edge scan on the deep archive.

WHY THIS IS NOT A REPEAT OF scan.py
-----------------------------------
scan.py's verdict -- 14.2M observations, nine cells clearing a 95% CI in-sample, ZERO
surviving the holdout -- is sound for the weather ladders that supply most of those
observations. It is NOT sound for the 15M series: `pull.py` caps at 600 markets and a
15M series runs 96 markets/day, so every 15M cell in that output rests on about EIGHT
days. `data_ohlc/` now holds 60,342 markets over 68 days, roughly 10x the history, for
the twelve series that are the only ones with minute-by-minute flow.

It also carries OHLC rather than closes alone, so the slicing can use things scan.py
could not represent: where the price sits inside the minute's own range, how wide that
range has been, and whether the book is leaning.

THE DISCIPLINE, WHICH IS THE WHOLE POINT
----------------------------------------
Hundreds of slices are tested, so ~5% of pure noise clears a 95% CI by construction.
Leads are therefore taken from the IN-SAMPLE period only, frozen, and then scored once
on a holdout that the search never saw. A lead is reported only if it ALSO:
  - clears zero on the holdout, and
  - replicates by SIGN across series, with the denominator printed.
Everything resamples close clusters (invariant 3: the series settle simultaneously).

    python3 research/search2/deep_scan.py
    python3 research/search2/deep_scan.py --split 0.6 --min-n 800
"""
import argparse, os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from maker_scan import load, fee_cents, bucket_price, bucket_secs  # noqa: E402
from maker_eval import cboot  # noqa: E402


def features(d):
    d = d.sort_values(["ticker", "side", "ts"]).copy()
    g = d.groupby(["ticker", "side"], sort=False)
    d["net"] = 100.0 * d.won.astype(float) - d.ask_c - d.ask_c.map(fee_cents)
    d["p1"] = g["ask_c"].shift(1); d["p2"] = g["ask_c"].shift(2); d["p3"] = g["ask_c"].shift(3)
    d["prior_min"] = d[["p1", "p2"]].min(axis=1)
    d["trend"] = d.ask_c - d.p3
    d["rng1"] = d.ask_h - d.bid_l
    d["rv3"] = g["rng1"].transform(lambda s: s.rolling(3, min_periods=3).mean())
    # where the close sits inside the minute's own trading range: 1.0 = closed on its
    # high (buyers took it up into the close), 0.0 = closed on its low.
    span = (d.px_h - d.px_l).replace(0, np.nan)
    d["clspos"] = ((d.px_c - d.px_l) / span).clip(0, 1)
    d["lean"] = (d.ask_c - d.px_c) - (d.px_c - d.bid_c)   # book leaning away from last trade
    return d


DIMS = {
    "series": lambda d: d.series,
    "price":  lambda d: bucket_price(d.ask_c),
    "secs":   lambda d: bucket_secs(d.secs_left),
    "side":   lambda d: d.side,
    "spread": lambda d: pd.cut(d.ask_c - d.bid_c, [0, 1.01, 3.01, 1e9],
                               labels=["tight<=1c", "mid2-3c", "wide>3c"]),
    "priors": lambda d: pd.cut(d.prior_min, [-1, 40, 60, 75, 90, 101],
                               labels=["pr<40", "pr40-59", "pr60-74", "pr75-89", "pr>=90"]),
    "trend":  lambda d: pd.cut(d.trend, [-1e9, -3, -1, 1, 3, 1e9],
                               labels=["fall>3", "fall1-3", "flat", "rise1-3", "rise>3"]),
    "rv3":    lambda d: pd.cut(d.rv3, [-1, 8, 14, 20, 1e9],
                               labels=["calm", "rv3 8-14", "rv3 14-20", "wild"]),
    "clspos": lambda d: pd.cut(d.clspos, [-.01, .25, .75, 1.01],
                               labels=["closed low", "mid", "closed high"]),
    "lean":   lambda d: pd.cut(d.lean, [-1e9, -1.01, 1.01, 1e9],
                               labels=["bid-heavy", "balanced", "ask-heavy"]),
}

PLANS = [["price"], ["price", "priors"], ["price", "trend"], ["price", "secs"],
         ["price", "rv3"], ["price", "clspos"], ["price", "lean"], ["price", "spread"],
         ["price", "side"], ["price", "priors", "trend"], ["price", "priors", "rv3"],
         ["price", "secs", "priors"], ["series", "price"], ["price", "clspos", "rv3"]]


def cells(d, dims, min_n, min_clusters):
    keys = [DIMS[x](d) for x in dims]
    d = d.assign(_k=list(zip(*[k.astype(str) for k in keys])))
    d = d[~d._k.map(lambda t: any(v in ("nan", "NaN") for v in t))]
    out = []
    for k, sub in d.groupby("_k", observed=True):
        if len(sub) < min_n or sub.close_ts.nunique() < min_clusters:
            continue
        lo, hi, p = cboot(sub, "net")
        out.append(dict(key=k, dims=tuple(dims), n=len(sub),
                        clusters=sub.close_ts.nunique(), net=sub.net.mean(),
                        lo=lo, hi=hi, p=p))
    return out


def score_on(d, lead, min_n=200):
    keys = [DIMS[x](d) for x in lead["dims"]]
    m = np.ones(len(d), bool)
    for k, want in zip(keys, lead["key"]):
        m &= (k.astype(str).values == want)
    sub = d[m]
    if len(sub) < min_n or sub.close_ts.nunique() < 30:
        return None
    lo, hi, p = cboot(sub, "net")
    return dict(n=len(sub), clusters=sub.close_ts.nunique(),
                net=sub.net.mean(), lo=lo, hi=hi, p=p, sub=sub)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--min-n", type=int, default=600)
    ap.add_argument("--min-clusters", type=int, default=40)
    ap.add_argument("--series", nargs="+")
    a = ap.parse_args()

    d = features(load(a.series))
    cl = np.sort(d.close_ts.unique()); cut = cl[int(len(cl) * a.split)]
    ins, out = d[d.close_ts < cut], d[d.close_ts >= cut]
    print(f"{len(d):,} quote-minutes | {d.close_ts.nunique():,} clusters | "
          f"{d.series.nunique()} series | {d.ticker.nunique():,} markets | "
          f"{(d.close_ts.max()-d.close_ts.min())/86400:.0f} days")
    print(f"IN-SAMPLE {len(ins):,} rows (< {a.split:.0%} of clusters) | "
          f"HOLDOUT {len(out):,} rows -- the search never sees the holdout\n")

    leads, tested = [], 0
    for dims in PLANS:
        c = cells(ins, dims, a.min_n, a.min_clusters)
        tested += len(c)
        leads += [x for x in c if x["lo"] is not None and x["lo"] > 0]
    print(f"{tested:,} cells tested in-sample; {len(leads)} cleared a 95% CI.")
    print(f"At a 5% level pure noise would produce about {tested*0.05:.0f} of them.\n")
    if not leads:
        print("No in-sample leads at all. Nothing to carry to the holdout.")
        return 0

    leads.sort(key=lambda x: -x["net"])
    print("HOLDOUT TEST -- leads frozen from in-sample, scored once on unseen clusters")
    print(f"  {'cell':<44}{'IS net':>8}{'OOS n':>8}{'OOS net':>9}{'OOS 95% CI':>18}{'P':>7}")
    print("  " + "-" * 100)
    survivors = []
    for L in leads[:40]:
        r = score_on(out, L)
        name = "/".join(L["key"])[:44]
        if r is None:
            print(f"  {name:<44}{L['net']:>+8.2f}{'—':>8}{'too few in holdout':>36}")
            continue
        mark = ""
        if r["lo"] > 0:
            mark = "  <-- SURVIVES"; survivors.append((L, r))
        print(f"  {name:<44}{L['net']:>+8.2f}{r['n']:>8,}{r['net']:>+9.2f}"
              f"   [{r['lo']:+6.2f},{r['hi']:+6.2f}]{r['p']:>7.3f}{mark}")

    print(f"\n{'='*102}\nSURVIVED THE HOLDOUT: {len(survivors)} of {len(leads)} leads")
    if not survivors:
        print("Nothing. Same verdict as the shallow scan, now on 10x the 15M history.")
        return 0

    print("\nREPLICATION BY SIGN, WITH THE DENOMINATOR (full window, both halves)")
    for L, r in survivors:
        name = "/".join(L["key"])
        full = score_on(d, L, min_n=100)
        print(f"\n  {name}   full-window n={full['n']:,} net={full['net']:+.2f}c "
              f"CI[{full['lo']:+.2f},{full['hi']:+.2f}]")
        pos = neg = 0
        for s, sub in full["sub"].groupby("series"):
            if len(sub) < 60:
                continue
            lo, hi, p = cboot(sub, "net")
            pos += sub.net.mean() > 0; neg += sub.net.mean() <= 0
            print(f"    {s:<14}n={len(sub):>6,}  net={sub.net.mean():>+7.2f}c  "
                  f"CI[{lo:+6.2f},{hi:+6.2f}]")
        print(f"    ==> {pos} positive / {neg} negative of {pos+neg} series")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
