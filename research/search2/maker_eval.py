#!/usr/bin/env python3
"""Step 5: evaluate ONE maker rule end to end, with the kill tests attached.

THE RULE UNDER TEST
-------------------
    On a 15M market, at the end of each minute, if the book is `spread >= 2c` and
    `bid_c + 1` lands in [LO, HI], rest a buy limit of N contracts at `bid_c + 1`
    for one minute, then cancel. Hold any fill to settlement. Never sell to close.

Every clause pays for itself:
  spread >= 2   an order at bid+1 must not cross the ask. Also the only state in
                which improving the quote is possible at all.
  bid_c + 1     step in FRONT of the resting queue. Joining the queue instead
                (`--join`) is measured too and is much worse: you then only fill once
                the level is CLEARED, which keeps the adverse fills and discards the
                benign ones.
  [LO, HI]      the favourite side. The longshot mirror of every cell here is
                negative, which is the same fact stated twice.
  secs > 120    drops fills inside the last two minutes so no result can be settlement
                foreknowledge. It costs edge -- the last two minutes are the WEAKEST
                part of the band -- so it is a conservatism, not a filter fitted for
                profit.
  N small       THE binding constraint, not a detail. Fill size scales with the
                minute's volume, and the minutes with the most volume are deep sweeps,
                which lose money. Let size follow volume and the same rule measures
                -5.05c; cap it at <=100 contracts and it measures +4.80c. The 10th
                percentile fill minute still carries ~580 contracts, so a small order
                fills completely in both regimes and the equal-weighted mean is then
                the honest number.

WHAT WOULD FALSIFY IT
---------------------
Adverse selection is the whole risk and it is visible in the data: fills where the
market traded 0-3c through the bid earn +12 to +16c, and fills where it traded 6c+
through -- 45% of them -- earn -3.8c. You cannot choose which you get; the tradeable
number is the blend. If the blend is negative on holdout, or the sign fails to
replicate across series, the rule is dead.

    python3 research/search2/maker_eval.py                  # full report
    python3 research/search2/maker_eval.py --lo 65 --hi 85 --size 50
"""
import argparse, glob, os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from maker_scan import load  # noqa: E402


def cboot(sub, col="net", iters=4000, seed=11):
    g = sub.groupby("close_ts")[col].agg(["sum", "count"])
    t = g["sum"].to_numpy(float); c = g["count"].to_numpy(float); n = len(t)
    if n < 30:
        return np.nan, np.nan, np.nan
    r = np.random.default_rng(seed)
    idx = r.integers(0, n, (iters, n))
    v = np.sort(t[idx].sum(1) / c[idx].sum(1))
    return v[int(.025 * iters)], v[int(.975 * iters)], float((v > 0).mean())


def add_rv3(d):
    """rv3 = mean intra-minute range over the PRECEDING three minutes.

    Strictly ex-ante: every input is closed by the time the order is placed at the end
    of minute t. This is the only filter here that replicated -- deep sweeps are
    volatility, and the sweep RATE is monotone in rv3 both in-sample and on a frozen
    holdout, which is a mechanical relationship rather than a P&L slice.
    """
    d = d.sort_values(["ticker", "side", "ts"])
    g = d.groupby(["ticker", "side"], sort=False)
    d["rng1"] = d.ask_h - d.bid_l
    d["rv3"] = g["rng1"].transform(lambda s: s.rolling(3, min_periods=3).mean())
    return d


def apply_rule(d, lo, hi, min_secs, join=False, size=50, rv3_max=None):
    d = add_rv3(d)
    if rv3_max is not None:
        d = d[d.rv3 <= rv3_max]
    d = d[d.spread >= 2].copy()
    d["B"] = d.bid_c if join else d.bid_c + 1
    if join:
        filled = ((d.n_px_l < d.B - 1e-9) | (d.n_bid_l < d.B - 1e-9)).fillna(False)
    else:
        filled = (d.n_px_l <= d.B + 1e-9).fillna(False)
    f = d[filled & d.B.between(lo, hi) & (d.secs_left > min_secs)].copy()
    f["net"] = 100 * f.won.astype(float) - f.B
    f["sweep"] = f.B - f.n_px_l
    f["qty"] = np.minimum(f.n_volume.fillna(0), size)
    return f


def line(tag, s, extra=""):
    if len(s) == 0:
        print(f"  {tag:<26} (no fills)"); return
    lo, hi, p = cboot(s)
    print(f"  {tag:<26}n={len(s):>6,} clus={s.close_ts.nunique():>5,} "
          f"net={s.net.mean():>+7.2f}c WR={s.won.mean():>5.1%} "
          f"CI[{lo:+6.2f},{hi:+6.2f}] P(>0)={p:.3f}{extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=float, default=65)
    ap.add_argument("--hi", type=float, default=85)
    ap.add_argument("--min-secs", type=int, default=120)
    ap.add_argument("--size", type=int, default=50)
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--join", action="store_true")
    ap.add_argument("--rv3-max", type=float, default=None,
                    help="only quote when the last 3 minutes were calm")
    ap.add_argument("--series", nargs="+")
    ap.add_argument("--data", default=os.path.join(HERE, "data_ohlc"))
    a = ap.parse_args()

    d = load(a.series, path=a.data)
    days = (d.close_ts.max() - d.close_ts.min()) / 86400
    print(f"{len(d):,} quote-minutes | {d.close_ts.nunique():,} clusters | "
          f"{d.series.nunique()} series | {d.ticker.nunique():,} markets | {days:.0f} days")
    f = apply_rule(d, a.lo, a.hi, a.min_secs, a.join, a.size, a.rv3_max)
    print(f"\nRULE: rest {a.size} contracts at bid+{0 if a.join else 1}, "
          f"{a.lo:.0f}-{a.hi:.0f}c, spread>=2c, >{a.min_secs}s left"
          + (f", rv3<={a.rv3_max:g}" if a.rv3_max else "") + ", hold to settlement")

    print("\n--- HEADLINE")
    line("all fills", f)

    print("\n--- OUT OF SAMPLE (split on close-cluster time, never on trades)")
    print("  Split is taken WITHIN each series, then pooled. A single pooled cut is a")
    print("  confound here: the series do not share a date span (ETH reaches back 67")
    print("  days, the metals ~25), so a pooled 'holdout' is also a different series")
    print("  MIX than the in-sample half, and the mix moves the result on its own.")
    parts_i, parts_o = [], []
    for _sn, _sub in f.groupby("series"):
        _cl = np.sort(_sub.close_ts.unique())
        _cut = _cl[int(len(_cl) * a.split)]
        parts_i.append(_sub[_sub.close_ts < _cut])
        parts_o.append(_sub[_sub.close_ts >= _cut])
    ins, out = pd.concat(parts_i), pd.concat(parts_o)
    line(f"in-sample first {a.split:.0%}", ins)
    line(f"HOLDOUT last {1-a.split:.0%}", out)

    print("\n--- REPLICATION, WITH THE DENOMINATOR")
    print("  (a leads list that prints only positives is not a sample -- CLAUDE.md,")
    print("   the 97-99c/prior>=90 error. Every series is listed, sign and all.)")
    pos = neg = 0
    for sn, sub in f.groupby("series"):
        if len(sub) < 60:
            print(f"  {sn:<26}n={len(sub):>6,}  too few fills to score"); continue
        line(sn, sub)
        pos += sub.net.mean() > 0; neg += sub.net.mean() <= 0
    print(f"  ==> {pos} positive / {neg} negative of {pos+neg} scored series")

    print("\n--- ADVERSE SELECTION (you do not get to choose which of these you get)")
    f["sb"] = pd.cut(f.sweep, bins=[-.01, 1.01, 3.01, 6.01, 1e9],
                     labels=["gentle 0-1c", "1-3c", "3-6c", "swept 6c+"])
    for lab, s in f.groupby("sb", observed=True):
        line(str(lab), s, f"  {len(s)/len(f):>5.1%} of fills")

    print("\n--- SIZE DISCIPLINE (the kill test this rule most nearly fails)")
    for cap in [10, 25, 50, 100, 250, 10**9]:
        w = np.minimum(f.n_volume.fillna(0), cap)
        v = (f.net * w).sum() / w.sum() if w.sum() else np.nan
        tag = "unlimited" if cap > 10**8 else str(cap)
        print(f"  size {tag:>9}  size-weighted net = {v:+.2f}c")

    print("\n--- CAPACITY")
    for sn, sub in f.groupby("series"):
        dd = max((sub.close_ts.max() - sub.close_ts.min()) / 86400, 1)
        fpd = len(sub) / dd
        print(f"  {sn:<14}{fpd:>6.1f} fills/day  x {a.size} ct x {sub.net.mean():+.2f}c "
              f"= ${fpd * a.size * sub.net.mean() / 100:>+8.2f}/day")
    tot = 0.0
    fpd_tot = 0.0
    for sn, sub in f.groupby("series"):
        dd = max((sub.close_ts.max() - sub.close_ts.min()) / 86400, 1)
        fpd_tot += len(sub) / dd
        tot += (len(sub) / dd) * a.size * sub.net.mean() / 100
    print(f"  {'TOTAL':<14}{fpd_tot:>6.1f} fills/day  -> ${tot:+.2f}/day "
          f"= ${tot*7:+.2f}/week")
    print("  (summed over per-series daily rates -- pooling fills across series with")
    print("   different date spans understates the rate and is not a portfolio figure)")
    print(f"  peak capital: {a.size} ct x ~{f.B.mean():.0f}c x concurrent positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
