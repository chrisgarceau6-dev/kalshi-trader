#!/usr/bin/env python3
"""Pooled strategy dissection with a genuine out-of-sample test.

THE IDEA, DONE RIGHT
--------------------
Not "copy wallet X's trades" and not "average everyone's trades into one
rule and test it on the same data" (that's memorization, not a strategy).

Instead: pool every round-trip from every wallet already tested tonight
(~250+ across multiple screening runs), tag each one by characteristics
that could plausibly generalize --  topic, entry-price band, hold-time
bucket -- and ask: does any (topic, price-band, hold-bucket) combination
show a real edge?

Then split ALL of this by TIME, not by wallet:
  TRAIN = round-trips with entry_ts before a cutoff date
  TEST  = round-trips with entry_ts after it, never touched during
          characteristic selection

Any rule is chosen using TRAIN only. It is then applied mechanically to
TEST. If the edge does not survive, that is the honest answer -- the
rule was pattern-matching noise in the training window, not a real
structural effect.

DATA SOURCE
-----------
Every wallet_5point*.py run left behind _tmp_<wallet>_0.0.csv files (the
0c-slippage round-trip CSVs) in this directory -- that's the raw
material. This script globs all of them.

usage:
    python strategy_dissect.py --probe           # see what's available
    python strategy_dissect.py --cutoff 2026-01-01
"""
import argparse, glob, re
import pandas as pd

TOPIC_RE = None  # topic already a column in these CSVs where present


def load_pool():
    files = glob.glob("_tmp_*_0.0.csv")
    frames = []
    for f in files:
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        if "entry_price" not in d.columns or "entry_ts" not in d.columns:
            continue
        d["_source_file"] = f
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    pool = pd.concat(frames, ignore_index=True)
    for c in ("entry_price", "exit_price", "hold_hrs"):
        if c in pool.columns:
            pool[c] = pd.to_numeric(pool[c], errors="coerce")
    pool["entry_ts"] = pd.to_datetime(pool["entry_ts"], errors="coerce")
    pool = pool.dropna(subset=["entry_price", "exit_price", "entry_ts"])
    return pool


def pnl(entry, exit_, slip=0.03, bet=100.0, fee_mult=0.05):
    e = min(max(entry + slip, 0.01), 0.99)
    x = min(max(exit_ - slip, 0.0), 1.0)
    ef = fee_mult * e * (1 - e)
    c = bet / (e + ef)
    return c * x - c * (e + ef)


def price_band(p):
    if p < 0.10: return "0-10c"
    if p < 0.25: return "10-25c"
    if p < 0.40: return "25-40c"
    if p < 0.60: return "40-60c"
    if p < 0.75: return "60-75c"
    if p < 0.90: return "75-90c"
    return "90-100c"


def hold_bucket(h):
    if pd.isna(h): return "unknown"
    if h < 1: return "<1h"
    if h < 6: return "1-6h"
    if h < 24: return "6-24h"
    if h < 168: return "1-7d"
    return ">7d"


def summarize(df, label):
    if df.empty:
        print(f"  [{label}] empty"); return None
    df = df.copy()
    df["pnl"] = [pnl(e, x) for e, x in zip(df.entry_price, df.exit_price)]
    s = df.pnl
    sharpe = s.mean() / s.std() if s.std() > 0 else float("nan")
    print(f"  [{label}] n={len(df)}  total_pnl=${s.sum():,.0f}  "
          f"mean=${s.mean():+.2f}  sharpe={sharpe:.3f}")
    return {"n": len(df), "total_pnl": s.sum(), "mean_pnl": s.mean(),
            "sharpe": sharpe}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--cutoff", default=None,
                   help="YYYY-MM-DD split date; default = median entry_ts")
    p.add_argument("--min-train-n", type=int, default=15,
                   help="ignore a bucket in training if it has fewer trades")
    a = p.parse_args()

    pool = load_pool()
    print(f"pooled {len(pool)} round-trips from "
          f"{pool['_source_file'].nunique() if len(pool) else 0} wallet files")
    if pool.empty:
        print("no _tmp_*_0.0.csv files found in this directory -- "
              "these are left over from wallet_5point.py runs; "
              "re-run a screening pass first if they were deleted")
        return

    if "topic" not in pool.columns:
        pool["topic"] = "unknown"
    pool["price_band"] = pool.entry_price.map(price_band)
    pool["hold_bucket"] = pool.hold_hrs.map(hold_bucket) if "hold_hrs" in pool else "unknown"

    if a.probe:
        print("\ndate range:", pool.entry_ts.min(), "to", pool.entry_ts.max())
        print("\ntopics:\n", pool.topic.value_counts())
        print("\nprice bands:\n", pool.price_band.value_counts())
        print("\nhold buckets:\n", pool.hold_bucket.value_counts())
        return

    cutoff = pd.Timestamp(a.cutoff) if a.cutoff else pool.entry_ts.median()
    train = pool[pool.entry_ts < cutoff]
    test = pool[pool.entry_ts >= cutoff]
    print(f"\ncutoff: {cutoff.date()}")
    print(f"train: {len(train)} round-trips  |  test: {len(test)} round-trips")

    print("\n=== whole-pool sanity check ===")
    summarize(train, "TRAIN, no filter")
    summarize(test, "TEST, no filter")

    print("\n=== searching TRAIN for a (topic, price_band, hold_bucket) "
          "combo with real edge ===")
    grp = train.groupby(["topic", "price_band", "hold_bucket"])
    candidates = []
    for key, g in grp:
        if len(g) < a.min_train_n:
            continue
        s = summarize(pd.DataFrame(), "")  # silence unused
        gg = g.copy()
        gg["pnl"] = [pnl(e, x) for e, x in zip(gg.entry_price, gg.exit_price)]
        sh = gg.pnl.mean() / gg.pnl.std() if gg.pnl.std() > 0 else 0
        if sh > 0.3:
            candidates.append((key, len(g), gg.pnl.sum(), sh))

    if not candidates:
        print("  no bucket cleared Sharpe > 0.3 in TRAIN with "
              f">= {a.min_train_n} trades. No rule to test OOS -- "
              "that itself is the finding: nothing generalizable emerged "
              "even before touching the test set.")
        return

    candidates.sort(key=lambda t: -t[3])
    print(f"\n  {len(candidates)} candidate rule(s) found in TRAIN:")
    for key, n, total, sh in candidates[:10]:
        print(f"    {key}  n={n}  train_pnl=${total:,.0f}  train_sharpe={sh:.3f}")

    print("\n=== applying TOP candidate rule to TEST (never seen) ===")
    top_key = candidates[0][0]
    mask_train = ((train.topic == top_key[0]) & (train.price_band == top_key[1])
                  & (train.hold_bucket == top_key[2]))
    mask_test = ((test.topic == top_key[0]) & (test.price_band == top_key[1])
                & (test.hold_bucket == top_key[2]))
    print(f"  rule: topic={top_key[0]}, price_band={top_key[1]}, "
          f"hold={top_key[2]}")
    summarize(train[mask_train], "same rule, TRAIN (in-sample)")
    r = summarize(test[mask_test], "same rule, TEST (out-of-sample)")

    print("\n--- VERDICT ---")
    if r is None or r["n"] < 5:
        print("  too few OOS trades to say anything (n<5) -- inconclusive, "
              "not a pass")
    elif r["sharpe"] > 0.3 and r["total_pnl"] > 0:
        print("  SURVIVED: this characteristic held up out-of-sample. "
              "Worth investigating further -- still not proof, just the "
              "first real signal after two nights of testing.")
    else:
        print("  DID NOT SURVIVE: looked good in-sample, failed OOS. "
              "This is the expected outcome for a pattern mined from "
              "noise -- the honest result, not a bug.")


if __name__ == "__main__":
    main()
