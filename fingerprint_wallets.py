#!/usr/bin/env python3
"""Behavioral strategy fingerprinting, with a wallet-level OOS split.

WHY THIS IS DIFFERENT FROM strategy_dissect.py
-----------------------------------------------
That script tagged trades by WHAT (topic) and WHEN (price/hold-time) and
pooled everyone equally, diluting any real signal from good wallets into
noise from mediocre ones. It also OOS-split by TIME, which only answers
"did this pattern exist in period A and period B", not "is this a real,
repeatable trait of skilled traders."

This script instead computes a BEHAVIORAL FINGERPRINT per wallet --
proxies for HOW they trade, not what they trade -- and splits by WALLET:
find the fingerprint of the better-performing half, then check if that
same fingerprint identifies better performers in the OTHER half of
wallets it never touched. That is the real test of "do good wallets
share a repeatable strategy trait."

FEATURES PER WALLET (all computable from round-trip data)
-----------------------------------------------------------
  momentum_score   : autocorrelation of entry price run-to-run for this
                      wallet. >0 = chases where price has been going,
                      <0 = buys against the recent trend (contrarian)
  size_cv           : coefficient of variation of position size --
                      low = fixed-size bettor, high = scales aggressively
  concentration_hhi : Herfindahl index over (topic, price_band) buckets
                      -- low = diversified, high = concentrated
  burstiness        : busiest-week trade share -- high = event-driven
                      bursts, low = steady activity
  median_hold_hrs   : kept as a feature, not just a filter
  win_rate          : fraction of round-trips with positive corrected pnl

Uses corrected Sharpe per wallet (recomputed here from the same
round-trip files) as the performance label.

usage:
    python fingerprint_wallets.py --probe
    python fingerprint_wallets.py
"""
import argparse, glob
import numpy as np
import pandas as pd


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


def fingerprint(df):
    d = df.copy().sort_values("entry_ts")
    d["pnl"] = [pnl(e, x) for e, x in zip(d.entry_price, d.exit_price)]
    d["price_band"] = d.entry_price.map(price_band)
    topic_col = d.topic if "topic" in d.columns else pd.Series(["unknown"] * len(d))

    mom = d.entry_price.autocorr(lag=1) if len(d) >= 5 else np.nan

    size_col = d["size"] if "size" in d.columns else None
    size_cv = (size_col.std() / size_col.mean()
              if size_col is not None and size_col.mean() else np.nan)

    combo = list(zip(topic_col, d.price_band))
    vc = pd.Series(combo).value_counts(normalize=True)
    hhi = (vc ** 2).sum()

    weeks = d.entry_ts.dt.to_period("W")
    wk_counts = weeks.value_counts()
    burst = wk_counts.max() / max(wk_counts.sum(), 1) if len(wk_counts) else np.nan

    sharpe = d.pnl.mean() / d.pnl.std() if d.pnl.std() > 0 else np.nan

    return {
        "n": len(d),
        "momentum_score": mom,
        "size_cv": size_cv,
        "concentration_hhi": hhi,
        "burstiness": burst,
        "median_hold_hrs": d.hold_hrs.median() if "hold_hrs" in d.columns else np.nan,
        "win_rate": (d.pnl > 0).mean(),
        "sharpe": sharpe,
        "total_pnl": d.pnl.sum(),
    }


def load_all():
    files = glob.glob("_tmp_*_0.0.csv")
    rows = []
    for f in files:
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        if "entry_price" not in d.columns or "entry_ts" not in d.columns:
            continue
        for c in ("entry_price", "exit_price", "hold_hrs", "size"):
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        d["entry_ts"] = pd.to_datetime(d["entry_ts"], errors="coerce")
        d = d.dropna(subset=["entry_price", "exit_price", "entry_ts"])
        if len(d) < 10:
            continue
        fp = fingerprint(d)
        fp["wallet_file"] = f
        rows.append(fp)
    return pd.DataFrame(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--min-n", type=int, default=10)
    a = p.parse_args()

    W = load_all()
    print(f"fingerprinted {len(W)} wallets (n>={a.min_n} round-trips each)")
    if W.empty:
        print("nothing to fingerprint"); return
    W = W.dropna(subset=["sharpe"])
    print(f"{len(W)} with a valid Sharpe")

    if a.probe:
        print("\nfeature summary:")
        print(W[["momentum_score", "size_cv", "concentration_hhi",
                 "burstiness", "median_hold_hrs", "win_rate", "sharpe"]]
              .describe().round(3))
        return

    W = W.sample(frac=1.0, random_state=42).reset_index(drop=True)
    half = len(W) // 2
    A, B = W.iloc[:half], W.iloc[half:]
    print(f"\nsplit: group A n={len(A)}, group B n={len(B)}")

    feats = ["momentum_score", "size_cv", "concentration_hhi",
            "burstiness", "median_hold_hrs", "win_rate"]

    print("\n=== correlation of each feature with Sharpe, GROUP A only ===")
    corrs = {}
    for f in feats:
        c = A[[f, "sharpe"]].dropna().corr().iloc[0, 1] if A[f].notna().sum() > 3 else np.nan
        corrs[f] = c
        print(f"  {f}: corr={c:+.3f}" if pd.notna(c) else f"  {f}: insufficient data")

    if not any(pd.notna(v) and abs(v) > 0.2 for v in corrs.values()):
        print("\nno feature shows even a modest correlation with Sharpe in "
              "group A. No fingerprint candidate to test on group B -- "
              "that is itself the finding.")
        return

    best_feat = max((f for f in corrs if pd.notna(corrs[f])),
                    key=lambda f: abs(corrs[f]))
    direction = corrs[best_feat] > 0
    thresh = A[best_feat].median()
    print(f"\nstrongest candidate: {best_feat} "
          f"(corr={corrs[best_feat]:+.3f}, direction={'high' if direction else 'low'} "
          f"is better, median split={thresh:.3f})")

    print(f"\n=== does '{best_feat} {'>' if direction else '<'} median' "
          f"predict better Sharpe in GROUP B (never touched)? ===")
    match = B[best_feat] > thresh if direction else B[best_feat] < thresh
    matched, unmatched = B[match], B[~match]
    print(f"  group B matching wallets   (n={len(matched)}): "
          f"mean_sharpe={matched.sharpe.mean():.3f}")
    print(f"  group B non-matching wallets(n={len(unmatched)}): "
          f"mean_sharpe={unmatched.sharpe.mean():.3f}")

    print("\n--- VERDICT ---")
    if len(matched) < 5 or len(unmatched) < 5:
        print("  too few wallets on one side to say anything -- inconclusive")
    elif matched.sharpe.mean() > unmatched.sharpe.mean() + 0.15:
        print(f"  SURVIVED: '{best_feat}' identified better-performing "
              "wallets in a group it never saw. This is a real candidate "
              "-- go look at what those matching wallets actually trade.")
    else:
        print(f"  DID NOT SURVIVE: '{best_feat}' looked predictive in group A, "
              "did not separate group B. Expected outcome for a feature "
              "correlated with Sharpe by chance in a finite sample.")


if __name__ == "__main__":
    main()
