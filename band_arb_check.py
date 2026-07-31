#!/usr/bin/env python3
"""Band-arbitrage check on a weather positions file.

THE IDEA
--------
Temperature-band markets for one city-day are mutually exclusive and
collectively exhaustive: exactly one band resolves YES. So the prices of
a COMPLETE band set must sum to $1.00. If you can buy the whole set for
less than $1.00 (minus fees), the profit is riskless — you are guaranteed
to hold exactly one winner.

Poligarch demonstrably buys every band in a city-day (London 24/25/26,
HK 32/33, NYC 86-87 and 88-89 ...). This asks whether those sets summed
to less than $1.00 at his entry prices.

SELF-VALIDATING COMPLETENESS TEST
---------------------------------
We cannot know from one wallet whether we are seeing every band. But we
do not have to guess: for a RESOLVED set, sum(curPrice) must equal ~1.00
(one band at 1, the rest at 0). So we only trust groups whose curPrice
sum is ~1.00 — those are provably complete. Everything else is reported
separately as incomplete/unresolved and excluded from the verdict.

THE HONEST CAVEAT
-----------------
avgPrice is HIS entry price, and the legs were bought at different times.
A sum below 1.00 therefore means he LEGGED INTO an arb over time, which
is a real (if harder) strategy — it is NOT proof that a simultaneous
riskless arb was sitting on the book. Distinguishing the two needs
order-book snapshots, which this repo does not have.

usage:
    python band_arb_check.py poli_weather_positions.csv
    python band_arb_check.py poli_weather_positions.csv --fee 0.02
"""
import argparse, re, sys
import pandas as pd

CITY_RE = re.compile(r"temperature in (.+?) be ", re.I)
DATE_RE = re.compile(r"on ([A-Z][a-z]+ \d{1,2})\??\s*$", re.I)


def key_of(title):
    t = str(title)
    c = CITY_RE.search(t)
    d = DATE_RE.search(t)
    if not c or not d:
        return None
    return f"{c.group(1).strip()} | {d.group(1).strip()}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv")
    p.add_argument("--fee", type=float, default=0.0,
                   help="round-trip fee+slippage cushion per set, in dollars")
    p.add_argument("--tol", type=float, default=0.03,
                   help="tolerance for calling sum(curPrice) complete")
    a = p.parse_args()

    try:
        d = pd.read_csv(a.csv)
    except FileNotFoundError:
        print(f"missing {a.csv}"); sys.exit(1)

    need = {"title", "avgPrice", "curPrice"}
    if not need.issubset(d.columns):
        print(f"need columns {need}, have {list(d.columns)}"); sys.exit(1)
    for c in ("avgPrice", "curPrice", "size"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    d["grp"] = d["title"].map(key_of)
    d = d.dropna(subset=["grp", "avgPrice", "curPrice"])
    print(f"{len(d)} band positions parsed into {d.grp.nunique()} city-day groups")

    g = d.groupby("grp").agg(
        bands=("avgPrice", "size"),
        entry_sum=("avgPrice", "sum"),
        cur_sum=("curPrice", "sum"),
    ).reset_index()

    complete = g[(g.cur_sum - 1.0).abs() <= a.tol].copy()
    other = g[(g.cur_sum - 1.0).abs() > a.tol]
    print(f"  provably COMPLETE resolved sets (sum curPrice ~1.00): {len(complete)}")
    print(f"  incomplete or unresolved (excluded):                  {len(other)}")

    if complete.empty:
        print("\nno complete sets — cannot test the arb from this wallet alone.")
        print("that itself is informative: he is not buying full band sets, or")
        print("the snapshot is missing the redeemed legs.")
        return

    complete["edge"] = 1.0 - complete["entry_sum"] - a.fee
    complete = complete.sort_values("edge", ascending=False)

    print("\n=== COMPLETE SETS: does the whole book cost less than $1.00? ===")
    with pd.option_context("display.width", 200, "display.max_colwidth", 40):
        print(complete[["grp", "bands", "entry_sum", "cur_sum", "edge"]]
              .head(25).to_string(index=False))

    n_arb = (complete.edge > 0).sum()
    print(f"\nsets priced below $1.00 after {a.fee*100:.0f}c cushion: "
          f"{n_arb} of {len(complete)} ({n_arb/len(complete):.0%})")
    print(f"mean entry_sum:   {complete.entry_sum.mean():.4f}")
    print(f"median entry_sum: {complete.entry_sum.median():.4f}")
    print(f"mean edge/set:    {complete.edge.mean()*100:+.2f}c")
    print(f"total if he bought $100 of each leg in every set: "
          f"${(complete.edge * 100 * complete.bands).sum():+,.0f}")

    print("\nREAD THIS BEFORE BELIEVING IT:")
    print("  entry_sum uses HIS fill prices across DIFFERENT times, so a sum")
    print("  below 1.00 shows he legged into it, not that a simultaneous")
    print("  riskless arb was resting on the book. Only live orderbook")
    print("  snapshots can tell you whether you could take it in one shot.")
    print("  Re-run with --fee 0.02 or 0.04 to see how much cushion it survives.")

    complete.to_csv("band_arb_sets.csv", index=False)
    print("\nsaved -> band_arb_sets.csv")


if __name__ == "__main__":
    main()
