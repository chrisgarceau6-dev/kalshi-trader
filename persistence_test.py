#!/usr/bin/env python3
"""
persistence_test.py — does wallet skill persist out-of-sample?

Input: a CSV of CLOSED trades, one row per closed position, with at least:
    - a wallet id column
    - a close-timestamp column (unix seconds OR ISO string)
    - a realized-pnl column (USD, signed)

The test:
  1. Bin trades into non-overlapping time blocks (default = 1 week).
  2. Per block, sum realized PnL per wallet, then rank wallets.
  3. Persistence via two independent measures:
       (a) Spearman rho between consecutive blocks' PnL, wallets present in both.
       (b) Transition prob: P(top-quintile in t+1 | top-quintile in t) vs 0.20 base rate.
  4. Re-run with an --exclude date window removed (kill the one-event dependency).

Verdict rule (edge is real only if it clears ALL of these, WITH the window excluded):
  - median consecutive-block rho >= 0.15 and > 0 in a majority of blocks
  - top-quintile transition prob materially above 0.20 (base rate for quintiles)
"""
import argparse, sys
import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def load(path, wallet_col, ts_col, pnl_col):
    df = pd.read_csv(path)
    for c in (wallet_col, ts_col, pnl_col):
        if c not in df.columns:
            sys.exit(f"ERROR: column '{c}' not in CSV. Found: {list(df.columns)}")
    df = df[[wallet_col, ts_col, pnl_col]].copy()
    df.columns = ["wallet", "ts", "pnl"]
    # timestamps: accept unix seconds or parseable strings
    if np.issubdtype(df["ts"].dtype, np.number):
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    return df.dropna(subset=["ts", "pnl"])


def block_pnl(df, freq):
    df = df.copy()
    df["block"] = df["ts"].dt.tz_localize(None).dt.to_period(freq)
    piv = df.groupby(["block", "wallet"])["pnl"].sum().reset_index()
    return piv


def run(piv, min_overlap=8):
    blocks = sorted(piv["block"].unique())
    rhos, trans_hits, trans_n = [], 0, 0
    for a, b in zip(blocks, blocks[1:]):
        pa = piv[piv["block"] == a].set_index("wallet")["pnl"]
        pb = piv[piv["block"] == b].set_index("wallet")["pnl"]
        common = pa.index.intersection(pb.index)
        if len(common) < min_overlap:
            continue
        rho, _ = spearmanr(pa[common], pb[common])
        if not np.isnan(rho):
            rhos.append(rho)
        # top-quintile transition among wallets present in both
        k = max(1, int(round(0.2 * len(common))))
        top_a = set(pa[common].sort_values(ascending=False).head(k).index)
        top_b = set(pb[common].sort_values(ascending=False).head(k).index)
        for w in top_a:
            trans_n += 1
            if w in top_b:
                trans_hits += 1
    rhos = np.array(rhos)
    trans = trans_hits / trans_n if trans_n else float("nan")
    return rhos, trans, trans_n


def report(tag, rhos, trans, trans_n):
    if len(rhos) == 0:
        print(f"[{tag}] not enough overlapping blocks to test."); return None
    med = np.median(rhos)
    frac_pos = float(np.mean(rhos > 0))
    print(f"\n=== {tag} ===")
    print(f"  consecutive-block pairs tested : {len(rhos)}")
    print(f"  median Spearman rho            : {med:+.3f}")
    print(f"  mean Spearman rho              : {rhos.mean():+.3f}")
    print(f"  blocks with rho > 0            : {frac_pos:.0%}")
    print(f"  top-quintile persistence       : {trans:.1%}  (random = 20.0%, n={trans_n})")
    passed = (med >= 0.15) and (frac_pos > 0.5) and (trans > 0.30)
    print(f"  VERDICT                        : {'PERSISTS' if passed else 'NO EDGE'}")
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--wallet-col", default="wallet")
    ap.add_argument("--ts-col", default="close_ts")
    ap.add_argument("--pnl-col", default="pnl")
    ap.add_argument("--freq", default="W", help="pandas period: W, 2W, M ...")
    ap.add_argument("--exclude", nargs=2, metavar=("START", "END"),
                    help="ISO dates to drop, e.g. 2025-06-10 2025-06-28")
    ap.add_argument("--min-overlap", type=int, default=8)
    args = ap.parse_args()

    df = load(args.csv, args.wallet_col, args.ts_col, args.pnl_col)
    print(f"loaded {len(df)} closed trades, "
          f"{df['ts'].min().date()} -> {df['ts'].max().date()}, "
          f"{df['wallet'].nunique()} wallets")

    piv = block_pnl(df, args.freq)
    r1 = run(piv, args.min_overlap)
    v_full = report("ALL DATA", *r1)

    if args.exclude:
        s, e = pd.to_datetime(args.exclude[0], utc=True), pd.to_datetime(args.exclude[1], utc=True)
        keep = df[(df["ts"] < s) | (df["ts"] > e)]
        print(f"\nexcluding {args.exclude[0]}..{args.exclude[1]}: "
              f"{len(df)-len(keep)} trades dropped")
        r2 = run(block_pnl(keep, args.freq), args.min_overlap)
        v_ex = report("EVENT WINDOW EXCLUDED", *r2)
        print("\n" + "="*46)
        if v_ex:
            print("Persistence survives event exclusion -> (b) worth building.")
        else:
            print("No persistence once the event is removed -> copy-trading is dead.")
            print("Do NOT deploy capital. Pivot to a structural edge or write it up.")


if __name__ == "__main__":
    main()
