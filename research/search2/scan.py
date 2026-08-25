#!/usr/bin/env python3
"""Step 4: systematic edge scan. The part that replaces guessing.

THE METHOD
----------
For every observation, the residual for a BUYER paying the ask is:

    residual = won - ask/100          (in probability)
    edge_c   = 100 * residual         (in cents/contract, since the payoff spread is 100c)

If the market is efficient that is zero everywhere. So instead of inventing a
hypothesis and testing it, slice the data every way available and ask which slices
have edge exceeding the fee at that price:

    NET = edge_c - fee(ask)      where fee(ask) = 0.07 * P * (1-P) * 100

Late-certainty is ONE CELL in that table. It should reappear unprompted — that is the
validation. If the scan cannot rediscover the strategy already known to work, it
cannot be trusted to find a new one.

WHY THE CIs ARE CLUSTERED
-------------------------
Markets sharing a close timestamp settle on the same event and are NOT independent —
a 6-strike weather ladder resolves on one temperature reading. Per-observation CIs
would treat 6 correlated outcomes as 6 draws and overstate significance badly. Every
interval here resamples CLOSE CLUSTERS (CLAUDE.md invariant 3).

MULTIPLE COMPARISONS ARE THE REAL RISK
--------------------------------------
This tests hundreds of slices. At a 5% level, ~5% of pure noise slices clear a 95% CI
by construction. So the output is a RANKED LEAD LIST, never a finding: every survivor
must go through step 5 (kill tests) and then be pre-registered and tested
out-of-sample before it means anything. The `--holdout` flag exists to make that easy.

    python3 research/search2/scan.py                    # everything
    python3 research/search2/scan.py --by series price  # custom slicing
    python3 research/search2/scan.py --holdout 0.3      # fit on 70%, report on 30%
"""
import argparse
import csv
import glob
import gzip
import math
import os
import random
import statistics
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

FEE_RATE = 0.07
MIN_CLUSTERS = 30          # below this a cluster bootstrap says nothing


def fee_cents(ask):
    p = ask / 100.0
    return FEE_RATE * p * (1 - p) * 100


def load(series_filter=None):
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "*.csv.gz"))):
        s = os.path.basename(path)[:-7]
        if series_filter and s not in series_filter:
            continue
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                try:
                    ask = float(r["ask"])
                    bid = float(r["bid"])
                except (TypeError, ValueError):
                    continue
                # A quote you cannot pay is not an observation. 0/100 means the side
                # is not on offer at all — the depth==0 confusion from the charter.
                if not (1.0 <= ask <= 99.0):
                    continue
                try:
                    rows.append(dict(
                        series=r["series"], ticker=r["ticker"],
                        close_ts=int(r["close_ts"]), secs=int(r["secs_left"]),
                        side=r["side"], bid=bid, ask=ask,
                        won=r["won"] in ("True", "true", "1"),
                        spread=round(ask - bid, 2),
                    ))
                except (TypeError, ValueError):
                    continue
    return rows


def bucket_price(a):
    if a < 10: return "01-09c"
    if a < 25: return "10-24c"
    if a < 40: return "25-39c"
    if a < 60: return "40-59c"
    if a < 75: return "60-74c"
    if a < 85: return "75-84c"
    if a < 90: return "85-89c"
    if a < 94: return "90-93c"
    if a < 97: return "94-96c"
    return "97-99c"


def bucket_secs(s):
    if s <= 300: return "0-5m"
    if s <= 900: return "5-15m"
    if s <= 1800: return "15-30m"
    if s <= 3600: return "30-60m"
    return "60m+"


DIMS = {
    "series": lambda r: r["series"],
    "price": lambda r: bucket_price(r["ask"]),
    "secs": lambda r: bucket_secs(r["secs"]),
    "side": lambda r: r["side"],
    "spread": lambda r: ("tight<=1c" if r["spread"] <= 1 else
                         "mid 1-3c" if r["spread"] <= 3 else "wide>3c"),
}


def bootstrap(by_cluster, iters=1500, seed=11):
    """Resample close clusters. Returns (lo, hi) of mean net edge in cents."""
    keys = list(by_cluster)
    if len(keys) < MIN_CLUSTERS:
        return None, None
    rnd = random.Random(seed)
    n = len(keys)
    out = []
    for _ in range(iters):
        tot = cnt = 0.0
        for _ in range(n):
            t, c = by_cluster[keys[rnd.randrange(n)]]
            tot += t
            cnt += c
        if cnt:
            out.append(tot / cnt)
    if not out:
        return None, None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def scan(rows, dims):
    cells = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    meta = defaultdict(lambda: [0, 0.0])
    for r in rows:
        key = tuple(DIMS[d](r) for d in dims)
        net = 100.0 * ((1.0 if r["won"] else 0.0) - r["ask"] / 100.0) - fee_cents(r["ask"])
        c = cells[key][r["close_ts"]]
        c[0] += net
        c[1] += 1.0
        meta[key][0] += 1
        meta[key][1] += r["ask"]
    out = []
    for key, by_cluster in cells.items():
        tot = sum(v[0] for v in by_cluster.values())
        cnt = sum(v[1] for v in by_cluster.values())
        if not cnt:
            continue
        lo, hi = bootstrap(by_cluster)
        n, sumask = meta[key]
        out.append(dict(key=key, n=n, clusters=len(by_cluster),
                        net=tot / cnt, lo=lo, hi=hi, avg_ask=sumask / n))
    return out


def report(title, res, min_n, min_clusters, top):
    sig = [r for r in res if r["n"] >= min_n and r["clusters"] >= min_clusters]
    sig.sort(key=lambda r: -r["net"])
    print(f"\n{title}  ({len(sig)} cells with n>={min_n} and >={min_clusters} clusters)")
    print(f"  {'cell':<44}{'n':>9}{'clus':>7}{'avg ask':>9}{'NET c/ct':>10}"
          f"   {'95% CI':>18}")
    print("  " + "-" * 100)
    shown = [r for r in sig[:top]]
    tail = [r for r in sig[-3:] if r not in shown]
    for r in shown + ([None] + tail if tail else []):
        if r is None:
            print("  " + "." * 100)
            continue
        ci = "—" if r["lo"] is None else f"[{r['lo']:+.2f}, {r['hi']:+.2f}]"
        mark = ""
        if r["lo"] is not None:
            if r["lo"] > 0:
                mark = "  <-- CI excludes 0"
            elif r["hi"] < 0:
                mark = "  <-- reliably NEGATIVE"
        print(f"  {' / '.join(map(str, r['key']))[:44]:<44}{r['n']:>9,}{r['clusters']:>7,}"
              f"{r['avg_ask']:>8.1f}c{r['net']:>+10.2f}   {ci:>18}{mark}")
    return sig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", nargs="+", default=None,
                    help=f"dimensions to slice by; any of {sorted(DIMS)}")
    ap.add_argument("--series", nargs="+")
    ap.add_argument("--min-n", type=int, default=400)
    ap.add_argument("--min-clusters", type=int, default=MIN_CLUSTERS)
    ap.add_argument("--top", type=int, default=18)
    ap.add_argument("--holdout", type=float, default=0.0,
                    help="report only the most recent fraction of clusters")
    a = ap.parse_args()

    rows = load(a.series)
    if not rows:
        raise SystemExit(f"no data in {DATA} — run pull.py first")
    if a.holdout > 0:
        cl = sorted({r["close_ts"] for r in rows})
        cut = cl[int(len(cl) * (1 - a.holdout))]
        rows = [r for r in rows if r["close_ts"] >= cut]
        print(f"HOLDOUT: most recent {a.holdout:.0%} of clusters only")
    ser = sorted({r["series"] for r in rows})
    print(f"{len(rows):,} observations | {len({r['close_ts'] for r in rows}):,} close "
          f"clusters | {len(ser)} series")
    print(f"NET = (won - ask) - fee(ask), in cents per contract. Positive means buying "
          f"at that ask was\nprofitable AFTER the exchange fee. CIs resample close "
          f"clusters, never observations.")

    plans = [a.by] if a.by else [["series"], ["price"], ["secs"],
                                 ["series", "price"], ["price", "secs"],
                                 ["series", "side"], ["price", "spread"]]
    allsig = []
    for dims in plans:
        res = scan(rows, dims)
        allsig += report(" x ".join(dims).upper(), res, a.min_n, a.min_clusters, a.top)

    winners = [r for r in allsig if r["lo"] is not None and r["lo"] > 0]
    print(f"\n{'='*102}")
    print(f"CELLS WHOSE 95% CI EXCLUDES ZERO: {len(winners)}")
    print("These are LEADS, not findings. This scan tested hundreds of slices, so ~5% "
          "of pure noise\nclears a 95% CI by construction. Every one must survive step "
          "5 (fee/population/depth/\ncapacity) and then be pre-registered with an "
          "out-of-sample window before it means anything.")
    for r in sorted(winners, key=lambda r: -r["net"])[:20]:
        print(f"  {' / '.join(map(str, r['key']))[:46]:<48}n={r['n']:>7,}  "
              f"net={r['net']:+.2f}c  CI[{r['lo']:+.2f},{r['hi']:+.2f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
