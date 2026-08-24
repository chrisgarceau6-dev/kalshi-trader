#!/usr/bin/env python3
"""$75 vs $100 sizing: drawdown, worst day, and stop-out risk on the 60-day sequence."""
import math
from decimal import Decimal, ROUND_CEILING

import numpy as np
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from no_60d import qualify, apply_cap

START_BALANCE = 1631.59
STOP_BALANCE = 650.0
RNG = np.random.default_rng(20260817)


def fee(price, n):
    raw = Decimal("0.07") * Decimal(n) * Decimal(str(price)) * (Decimal("1") - Decimal(str(price)))
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def pnl_at(row, budget):
    eff = (row["ask"] + 1.0) / 100.0
    n = math.floor(budget / eff)
    spend = n * eff + fee(eff, n)
    return (n - spend) if row["won"] else -spend


def sequence(yes, budget):
    """Chronological per-cluster P&L (clusters settle simultaneously)."""
    p = yes.apply(lambda r: pnl_at(r, budget), axis=1)
    df = yes.assign(pnl=p)
    return df.groupby("close_ts").pnl.sum().sort_index(), df


def drawdown(equity):
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    return dd.min(), (dd / peak).min()


def report(yes, budget):
    per_cluster, df = sequence(yes, budget)
    eq = START_BALANCE + per_cluster.cumsum().values
    dd_abs, dd_pct = drawdown(eq)
    day = df.assign(d=pd.to_datetime(df.close_ts, unit="s").dt.date).groupby("d").pnl.sum()
    breach = (eq <= STOP_BALANCE).any()
    print(f"\n--- ${budget:.0f}/trade " + "-" * 46)
    print(f"  trades                 {len(df):,}")
    print(f"  net P&L 60d            ${per_cluster.sum():+,.0f}   (${per_cluster.sum()/60.1:+.0f}/day)")
    print(f"  $/trade                ${df.pnl.mean():+.2f}")
    print(f"  ending equity          ${eq[-1]:,.0f}")
    print(f"  max drawdown           ${dd_abs:,.0f}   ({dd_pct*100:.1f}% of peak)")
    print(f"  worst single day       ${day.min():,.0f}")
    print(f"  worst cluster          ${per_cluster.min():,.0f}")
    print(f"  losing days            {(day < 0).sum()} of {len(day)}")
    print(f"  days worse than -$300  {(day < -300).sum()}")
    print(f"  days worse than -$400  {(day < -400).sum()}")
    print(f"  hits ${STOP_BALANCE:.0f} stop?      {'YES' if breach else 'no'}")
    print(f"  per-trade % of equity  {budget/START_BALANCE*100:.1f}%   "
          f"(2 concurrent = {2*budget/START_BALANCE*100:.1f}%)")
    return per_cluster, day, eq


def main():
    d = pd.read_csv("/Users/chrisgarceau/pm/backtest_ablation_raw.csv").dropna(subset=["prior_1", "prior_2"])
    sig = qualify(d, live_prior3_gate=True)
    yes = apply_cap(sig[sig.side == "yes"])
    print(f"60-day sequence, live v5.15 rule. Start balance ${START_BALANCE:,.2f}, stop ${STOP_BALANCE:.0f}.")

    out = {}
    for b in (75, 100):
        out[b] = report(yes, b)

    pc75, day75, eq75 = out[75]
    pc100, day100, eq100 = out[100]
    print("\n" + "=" * 60)
    print(f"  extra profit at $100   ${pc100.sum()-pc75.sum():+,.0f} over 60 days "
          f"(${(pc100.sum()-pc75.sum())/60.1:+.2f}/day)")
    print(f"  extra max drawdown     ${drawdown(eq100)[0]-drawdown(eq75)[0]:+,.0f}")
    print(f"  extra worst day        ${day100.min()-day75.min():+,.0f}")

    # block bootstrap over contiguous days: how bad can a 60-day run get?
    print("\n  Block bootstrap (1,000 resampled 60-day runs, 3-day blocks):")
    for b, (pc, day, _) in out.items():
        vals = day.values
        n = len(vals)
        worst_dd, ruin = [], 0
        for _ in range(1000):
            idx = []
            while len(idx) < n:
                s = RNG.integers(0, n - 3)
                idx += list(range(s, s + 3))
            path = START_BALANCE + np.cumsum(vals[idx[:n]])
            worst_dd.append(drawdown(path)[0])
            if (path <= STOP_BALANCE).any():
                ruin += 1
        print(f"    ${b}: median worst DD ${np.median(worst_dd):,.0f}  "
              f"5th pct ${np.percentile(worst_dd,5):,.0f}  "
              f"P(hit ${STOP_BALANCE:.0f} stop) {ruin/1000*100:.1f}%")


if __name__ == "__main__":
    main()
