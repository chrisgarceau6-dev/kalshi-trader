#!/usr/bin/env python3
"""Sanity-check the screener's SCORING LOGIC, not a real wallet.

THE QUESTION
------------
Every real wallet tonight has failed. Is that because there's genuinely
no edge out there, or because the scoring logic itself has a bug that
would fail ANY wallet, good or bad?

THE TEST
--------
Generate a synthetic round-trip history that is, by construction,
unambiguously good:
  - real edge that survives 3c slippage (not just before-cost noise)
  - spread over 15 months, no month >40% of volume
  - hold times all >24h
  - <=15 concurrent positions
  - n=250, comfortably above the sample floor

Run the EXACT same pnl/correction/scoring functions used by
wallet_5point2.py against this synthetic data. If this fails, the bug
is in the tool. If it passes, the tool works and tonight's real results
are the honest answer.

usage:
    python validate_screener.py
"""
import numpy as np
import pandas as pd


def pnl(entry, exit_, slip=0.03, bet=100.0, fee_mult=0.05):
    e = min(max(entry + slip, 0.01), 0.99)
    x = min(max(exit_ - slip, 0.0), 1.0)
    ef = fee_mult * e * (1 - e)
    c = bet / (e + ef)
    return c * x - c * (e + ef)


def build_synthetic(n=250, months=15, win_rate=0.62, seed=7):
    """A genuinely good, well-formed track record, built by hand so we
    know the ground truth: real edge, spread out, realistic hold times."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2025-01-01")
    rows = []
    for i in range(n):
        # spread entries roughly evenly across `months`, with mild
        # randomness so no single month dominates
        day_offset = rng.uniform(0, months * 30)
        entry_ts = start + pd.Timedelta(days=day_offset)
        entry_price = rng.uniform(0.35, 0.55)   # moderate, not thin tails
        won = rng.random() < win_rate
        exit_price = 1.0 if won else 0.0
        hold_hrs = rng.uniform(30, 300)          # comfortably >24h
        rows.append({"entry_ts": entry_ts, "entry_price": entry_price,
                    "exit_price": exit_price, "hold_hrs": hold_hrs})
    return pd.DataFrame(rows)


def score(df, label, bet=100.0, min_hold_hrs=24.0, max_concurrent=20,
          sharpe_bar=0.15):
    d = df.copy()
    d["pnl_0c"] = [pnl(e, x, slip=0.0, bet=bet) for e, x in zip(d.entry_price, d.exit_price)]
    d["pnl_3c"] = [pnl(e, x, slip=0.03, bet=bet) for e, x in zip(d.entry_price, d.exit_price)]

    pnl_0c_total = d.pnl_0c.sum()
    pnl_3c_total = d.pnl_3c.sum()
    edge_ratio = (pnl_3c_total / pnl_0c_total) if pnl_0c_total > 0 else None

    s = d.pnl_3c
    sharpe = s.mean() / s.std() if s.std() > 0 else float("nan")

    med_hold = d.hold_hrs.median()

    by_month = d.groupby(d.entry_ts.dt.to_period("M")).size()
    n_months = by_month.index.nunique()
    top_month_share = by_month.max() / by_month.sum()

    # crude concurrency proxy: max overlapping holds at once
    intervals = sorted(zip(d.entry_ts, d.entry_ts + pd.to_timedelta(d.hold_hrs, unit="h")))
    events = []
    for st, en in intervals:
        events.append((st, 1)); events.append((en, -1))
    events.sort()
    cur = maxc = 0
    for _, delta in events:
        cur += delta; maxc = max(maxc, cur)

    checks = {
        "1_edge_vs_spread [HARD]": edge_ratio is not None and edge_ratio >= 0.5,
        "2_hold_ge_24h [SOFT]": med_hold >= min_hold_hrs,
        f"3_corrected_sharpe_gt_{sharpe_bar} [HARD]": (
            pd.notna(sharpe) and sharpe > sharpe_bar),
        "4_sample_and_spread [HARD]": (len(d) >= 200 and n_months >= 12
                                       and top_month_share <= 0.4),
        "5_concurrent_le_N [SOFT]": maxc <= max_concurrent,
    }

    print(f"=== {label} ===")
    print(f"n={len(d)}  pnl_0c=${pnl_0c_total:,.0f}  pnl_3c=${pnl_3c_total:,.0f}  "
          f"edge_ratio={edge_ratio if edge_ratio is None else round(edge_ratio,2)}  "
          f"sharpe={sharpe:.3f}")
    print(f"median_hold={med_hold:.1f}h  months={n_months}  "
          f"top_month_share={top_month_share:.2f}  max_concurrent={maxc}")
    print("--- scorecard ---")
    for k, v in checks.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    hard = [k for k in checks if "[HARD]" in k]
    hard_passed = sum(checks[k] for k in hard)
    print(f"{hard_passed}/{len(hard)} HARD criteria met, "
          f"{sum(checks.values())}/{len(checks)} total\n")
    return hard_passed == len(hard)


def build_bad_synthetic(n=250, months=15, win_rate=0.45, seed=13):
    """A genuinely BAD wallet -- 45% win rate is a losing proposition
    even before slippage. This should FAIL, and needs to keep failing
    after the fix, or we've just made the bar meaningless."""
    return build_synthetic(n=n, months=months, win_rate=win_rate, seed=seed)


def main():
    print("=== TEST 1: obviously GOOD synthetic wallet ===")
    print("(62% win rate, moderate prices, 15mo spread, real hold times)\n")
    good = build_synthetic()
    good_passed = score(good, "GOOD wallet (expect PASS)")

    print("=== TEST 2: obviously BAD synthetic wallet (control) ===")
    print("(45% win rate -- a losing proposition, should still FAIL)\n")
    bad = build_bad_synthetic()
    bad_passed = score(bad, "BAD wallet (expect FAIL)")

    print("--- FINAL VERDICT ---")
    if good_passed and not bad_passed:
        print("Fix confirmed correct: the good wallet now PASSES, and the")
        print("bad wallet still correctly FAILS. The bar is calibrated --")
        print("it's discriminating real edge from no edge, not just always")
        print("passing everything.")
    elif good_passed and bad_passed:
        print("PROBLEM: both wallets passed. The bar is now too loose --")
        print("it would pass a genuinely bad wallet too. Needs tightening,")
        print("not loosening further.")
    elif not good_passed:
        print("Still failing the good wallet -- the fix didn't take, or")
        print("another criterion is also miscalibrated. Check the scorecard")
        print("above for which specific line failed.")


if __name__ == "__main__":
    main()
