#!/usr/bin/env python3
"""Step 6: portfolio simulation. Turns cents-per-contract into dollars and drawdown.

A per-contract edge is not a strategy. This walks the fills in time order, enforces a
capital ceiling and a per-cluster exposure cap, and reports the day-by-day P&L series
-- which is the only form in which the question "is this worth running" can be asked.

TWO CONSTRAINTS THAT ARE NOT OPTIONAL
-------------------------------------
1. CAPITAL. Fills are held to settlement, so capital is tied up from fill to close.
   Positions here live minutes, so the ceiling binds far less than the fill count
   suggests -- but it has to be enforced rather than assumed, and an order that cannot
   be funded is not taken.
2. CLUSTER EXPOSURE. CLAUDE.md invariant 3: the 15M series settle SIMULTANEOUSLY and
   are one correlated bet, not many. Concurrent positions across series resolve
   together, so the cap that matters is per CLOSE CLUSTER, not per position.

The daily series is then bootstrapped by DAY, not by trade, for the same reason.

    python3 research/search2/maker_sim.py --size 25 --max-cluster 8
"""
import argparse, os, sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from maker_scan import load          # noqa: E402
from maker_eval import apply_rule    # noqa: E402


def simulate(f, size, max_cluster, capital):
    """Walk fills in time order. Returns the taken fills with capital accounting."""
    f = f.sort_values("ts").copy()
    f["cost"] = f.B / 100.0 * size
    f["payoff"] = f.won.astype(float) * size
    taken, per_cluster, open_pos = [], {}, []
    used = 0.0
    peak = [0.0]
    for r in f.itertuples():
        # release capital from anything that has settled by now
        while open_pos and open_pos[0][0] <= r.ts:
            used -= open_pos.pop(0)[1]
        c = per_cluster.get(r.close_ts, 0)
        if c >= max_cluster:
            continue
        if used + r.cost > capital:
            continue
        per_cluster[r.close_ts] = c + 1
        used += r.cost
        open_pos.append((r.close_ts, r.cost))
        open_pos.sort(key=lambda x: x[0])
        peak[0] = max(peak[0], used)
        taken.append(r.Index)
    t = f.loc[taken].copy()
    t.attrs["peak_capital"] = peak[0]
    t["pnl"] = t.payoff - t.cost
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=float, default=65)
    ap.add_argument("--hi", type=float, default=85)
    ap.add_argument("--min-secs", type=int, default=120)
    ap.add_argument("--rv3-max", type=float, default=18)
    ap.add_argument("--size", type=int, default=25)
    ap.add_argument("--max-cluster", type=int, default=8)
    ap.add_argument("--capital", type=float, default=2000.0)
    ap.add_argument("--series", nargs="+")
    a = ap.parse_args()

    d = load(a.series)
    f = apply_rule(d, a.lo, a.hi, a.min_secs, False, a.size, a.rv3_max)
    t = simulate(f, a.size, a.max_cluster, a.capital)
    print(f"{len(f):,} eligible fills -> {len(t):,} taken "
          f"({len(t)/max(len(f),1):.1%}) under ${a.capital:,.0f} cap, "
          f"max {a.max_cluster}/cluster, {a.size} ct")

    t["day"] = pd.to_datetime(t.close_ts, unit="s", utc=True).dt.tz_convert(
        "America/New_York").dt.date
    g = t.groupby("day").agg(n=("pnl", "size"), pnl=("pnl", "sum"))
    # Only score days every series could contribute to; a partial first/last day
    # understates volume and flatters or punishes the mean at random.
    g = g.iloc[1:-1]
    print(f"\n{len(g)} full days | {g.n.mean():.0f} fills/day | "
          f"mean ${g.pnl.mean():+.2f}/day | sd ${g.pnl.std(ddof=1):.2f}/day")
    print(f"median ${g.pnl.median():+.2f} | worst ${g.pnl.min():+.2f} | "
          f"best ${g.pnl.max():+.2f} | {int((g.pnl>0).sum())}/{len(g)} days positive")

    rng = np.random.default_rng(3)
    v = g.pnl.to_numpy(float)
    bs = rng.choice(v, (20000, len(v))).mean(axis=1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    print(f"\nDAY-BOOTSTRAPPED mean: ${g.pnl.mean():+.2f}/day  "
          f"95% CI [${lo:+.2f}, ${hi:+.2f}]  P(>0)={float((bs>0).mean()):.3f}")
    print(f"WEEKLY: ${g.pnl.mean()*7:+.2f}/week  "
          f"95% CI [${lo*7:+.2f}, ${hi*7:+.2f}]   target is +$250/week")

    eq = g.pnl.cumsum()
    dd = (eq.cummax() - eq).max()
    print(f"\nworst peak-to-trough drawdown over the window: ${dd:,.2f}")
    print(f"PEAK CONCURRENT capital actually at risk: ${t.attrs['peak_capital']:,.0f} "
          f"of the ${a.capital:,.0f} ceiling")
    print(f"  (turnover is ${t.cost.sum()/max(len(g),1):,.0f}/day, but positions live "
          f"minutes, so the\n   ceiling binds far less than turnover suggests -- "
          f"this is the number that binds)")

    print("\nPOWER: days needed to call this, at the measured daily sd")
    sd = g.pnl.std(ddof=1); m = g.pnl.mean()
    if m > 0:
        n = (2.8 * sd / m) ** 2
        print(f"  to separate ${m:+.2f}/day from $0 at 95%/80%: {n:.0f} trading days "
              f"({n/7:.0f} weeks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
