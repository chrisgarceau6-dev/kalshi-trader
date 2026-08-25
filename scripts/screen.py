#!/usr/bin/env python3
"""Rank markets by NET edge per contract: measured mispricing minus the Kalshi fee.

WHY THIS EXISTS
---------------
The search for a second strategy has been idea-first — think of something, spend a
week testing it. Thirty ideas, one marginal survivor, ~3% hit rate. That is sampling
an unbounded space.

It did not have to be. Kalshi's fee is 0.07*C*P*(1-P), a PARABOLA MAXIMISED AT 50c:

    50c -> 1.750c/contract      92c -> 0.515c/contract

So the fee structure structurally penalises direction-neutral trades and subsidises
tail trades. Every one of the 11 direction-neutral structures in CLAUDE.md's kill list
died to the same arithmetic — a market-neutral pair pays 3.4x the fee for an edge that
is smaller by construction, because neutrality gave up the direction. That was
computable before any of them were tested.

The live strategy is not "buy high confidence". It is "trade where mispricing exceeds
the fee at that price". This script measures both terms on the same axis, per series
and per price, so a candidate can be killed by arithmetic instead of by a week.

    net_edge(price) = measured_mispricing(price) - fee(price)

VALIDATION FIRST
----------------
Run with no arguments and it screens the archived series. It must independently
rediscover what is already known: the 90-93c band positive, 95c+ negative, 94c
positive-but-unexploited. If it does not reproduce that, do not point it at anything
new.

    python3 scripts/screen.py                 # archived series, by price bucket
    python3 scripts/screen.py --by-series     # rank series at the live band
    python3 scripts/screen.py --min-n 200     # require a sample size

CAVEATS THAT ARE NOT OPTIONAL
-----------------------------
- The archive is integer-rounded before 2026-08-22, which biases selection optimistic
  (docs/audit/claude/CLAIMS.md §3). Use --since 2026-08-22 for exact prices, at the
  cost of sample size.
- This is a SCREEN, not evidence. Ranking N series is N chances to find noise. Nothing
  here is actionable until it is pre-registered and tested out-of-sample, exactly like
  v5.17. The CI column exists so you can see how little most rows say.
- Mispricing measured on the archive is an upper bound on what is capturable: it sees
  every candle, the bot sees poll instants (invariant 6), and it cannot model the book
  depth gate. A row with edge but no depth is the ask-94 failure.
"""
import argparse
import csv
import glob
import gzip
import math
import os
import random
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "candles")

FEE_RATE = 0.07


def fee_cents(price_cents):
    """Kalshi's per-contract fee at this price, in cents. A parabola peaking at 50c."""
    p = price_cents / 100.0
    return FEE_RATE * p * (1 - p) * 100


def load(since=None, until=None, series=None):
    """One row per (ticker, side, candle). Prices are exact cents where available."""
    rows, seen = [], set()
    for path in sorted(glob.glob(os.path.join(DATA, "*.csv.gz"))):
        day = os.path.basename(path)[:10]
        if (since and day < since) or (until and day > until):
            continue
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                if series and r["series"] not in series:
                    continue
                k = (r["ticker"], r["side"], r["candle_idx"])
                if k in seen:
                    continue
                seen.add(k)
                try:
                    rows.append((r["series"], int(r["close_ts"]), r["side"],
                                 float(r["ask"]), int(r["secs_left"]),
                                 r["won"] in ("True", "true", "1")))
                except (TypeError, ValueError):
                    continue
    return rows


def cluster_bootstrap(by_cluster, iters=2000, seed=7):
    """Resample close clusters, not observations — the series settle together, so a
    close timestamp is ONE draw, never seven (CLAUDE.md invariant 3). Per-trade CIs
    here are wrong and will overstate significance badly."""
    keys = list(by_cluster)
    if len(keys) < 8:
        return None, None
    rnd = random.Random(seed)
    n = len(keys)
    out = []
    for _ in range(iters):
        num = den = 0.0
        for _ in range(n):
            w, c = by_cluster[keys[rnd.randrange(n)]]
            num += w
            den += c
        if den:
            out.append(num / den)
    if not out:
        return None, None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def measure(rows, bucket_fn):
    """Mispricing per bucket, cluster-robust.

    An extra 1pp of win rate is worth ~1c/contract: winning pays (100 - ask) and
    losing costs (ask), so the spread is 100c and edge_in_cents == edge_in_pp.
    """
    buckets = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    n = defaultdict(int)
    px = defaultdict(float)
    for series, cts, side, ask, secs, won in rows:
        b = bucket_fn(series, ask, secs, side)
        if b is None:
            continue
        cell = buckets[b][cts]
        cell[0] += (1.0 if won else 0.0) - ask / 100.0   # realised minus implied
        cell[1] += 1.0
        n[b] += 1
        px[b] += ask
    out = {}
    for b, by_cluster in buckets.items():
        num = sum(v[0] for v in by_cluster.values())
        den = sum(v[1] for v in by_cluster.values())
        if not den:
            continue
        edge = 100 * num / den                            # pp, == cents/contract
        lo, hi = cluster_bootstrap(by_cluster)
        avg_px = px[b] / n[b]
        out[b] = dict(n=n[b], clusters=len(by_cluster), edge=edge,
                      lo=None if lo is None else 100 * lo,
                      hi=None if hi is None else 100 * hi,
                      avg_px=avg_px, fee=fee_cents(avg_px))
    return out


def render(title, table, min_n, sort_by_net=False):
    print(f"\n{title}")
    print(f"  {'bucket':<16}{'n':>7}{'clus':>7}{'avg px':>9}"
          f"{'edge':>9}{'fee':>8}{'NET':>9}   {'95% CI (cluster)':>22}")
    print("  " + "-" * 94)
    items = [(k, v) for k, v in table.items() if v["n"] >= min_n]
    items.sort(key=(lambda kv: -(kv[1]["edge"] - kv[1]["fee"])) if sort_by_net
               else (lambda kv: kv[0]))
    for k, v in items:
        net = v["edge"] - v["fee"]
        ci = ("—" if v["lo"] is None
              else f"[{v['lo'] - v['fee']:+.2f}, {v['hi'] - v['fee']:+.2f}]")
        flag = ""
        if v["lo"] is not None:
            if v["lo"] - v["fee"] > 0:
                flag = "  <-- CI excludes 0"
            elif v["hi"] - v["fee"] < 0:
                flag = "  <-- dead"
        print(f"  {str(k):<16}{v['n']:>7,}{v['clusters']:>7,}{v['avg_px']:>8.2f}c"
              f"{v['edge']:>+9.2f}{v['fee']:>8.3f}{net:>+9.2f}   {ci:>22}{flag}")
    skipped = len(table) - len(items)
    if skipped:
        print(f"  ({skipped} bucket(s) below --min-n {min_n} not shown)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="YYYY-MM-DD. Use 2026-08-22 for exact-cent prices.")
    ap.add_argument("--until")
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--by-series", action="store_true",
                    help="rank series at the live band instead of bucketing by price")
    ap.add_argument("--secs", nargs=2, type=int, default=[150, 600],
                    metavar=("LO", "HI"))
    a = ap.parse_args()

    rows = load(a.since, a.until)
    if not rows:
        raise SystemExit("no rows in that window")
    lo, hi = a.secs
    days = (max(r[1] for r in rows) - min(r[1] for r in rows)) / 86400
    print(f"{len(rows):,} observations | {len({r[1] for r in rows}):,} clusters | "
          f"{len({r[0] for r in rows})} series | {days:.0f} days | secs {lo}-{hi}")
    print("edge and fee are both cents/contract, so NET is directly comparable across "
          "prices.")
    if not a.since or a.since < "2026-08-22":
        print("NOTE: window includes integer-rounded days (pre 2026-08-22); selection "
              "is biased optimistic.\n      Re-run --since 2026-08-22 to check a "
              "conclusion on exact prices.")

    if a.by_series:
        t = measure(rows, lambda s, ask, secs, side:
                    s if (lo <= secs <= hi and 88 <= ask <= 96) else None)
        render("NET edge by series, ask 88-96c", t, a.min_n, sort_by_net=True)
    else:
        t = measure(rows, lambda s, ask, secs, side:
                    int(ask) if lo <= secs <= hi else None)
        render("NET edge by ask price", t, a.min_n)
        print("\n  VALIDATION: this must reproduce the known answer — 90-93c positive "
              "(the live band),\n  95c+ negative, 94c positive. If it does not, the "
              "method is broken; do not trust it elsewhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
