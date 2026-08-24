#!/usr/bin/env python3
"""How the earliest-side YES/NO policy would have done over the 60-day window.

Replicates the execution model in the Codex holdout audit exactly:
  effective fill = signal ask + 1 cent adverse
  contracts     = floor(75 / effective)
  fee           = ceil(0.07 * contracts * p * (1-p), 2dp)
Cluster = close_ts (all series settle simultaneously) -> bootstrap on clusters.
"""
import math
from decimal import Decimal, ROUND_CEILING

import numpy as np
import pandas as pd

BUDGET = 75.0
SERIES_ORDER = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
RNG = np.random.default_rng(20260817)


def fee(price, contracts):
    raw = Decimal("0.07") * Decimal(contracts) * Decimal(str(price)) * (Decimal("1") - Decimal(str(price)))
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def price_signal(row):
    eff = (row["ask"] + 1.0) / 100.0
    contracts = math.floor(BUDGET / eff)
    f = fee(eff, contracts)
    spend = contracts * eff + f
    pnl = (contracts - spend) if row["won"] else -spend
    return contracts, f, round(pnl, 6)


def qualify(d, live_prior3_gate):
    """First qualifying candle per (ticker, side), earliest in time."""
    ok_prior = (d.prior_1 >= 75) & (d.prior_2 >= 75)
    window = d.secs_left.between(150, 600)

    yes = d[(d.side == "yes") & d.ask.between(90, 93) & ok_prior & window].copy()
    if live_prior3_gate:  # live v5.15 rule: ask<=91 also needs prior_3>=80
        yes = yes[(yes.ask >= 92) | (yes.prior_3 >= 80)]

    no = d[(d.side == "no") & d.ask.between(90, 91) & ok_prior & window].copy()

    sig = pd.concat([yes, no], ignore_index=True)
    sig = sig[sig.series.isin(SERIES_ORDER)]
    sig["series_rank"] = sig.series.map({s: i for i, s in enumerate(SERIES_ORDER)})
    # earliest signal = largest secs_left
    sig = sig.sort_values(["ticker", "side", "secs_left"], ascending=[True, True, False])
    return sig.groupby(["ticker", "side"], as_index=False).first()


def apply_cap(sig, cap=2):
    """Earliest signals fill a global cap per close-time cluster."""
    s = sig.sort_values(["close_ts", "secs_left", "series_rank"], ascending=[True, False, True])
    return s.groupby("close_ts", as_index=False).head(cap)


def earliest_side(sig):
    """One side per market: whichever qualified first. Ties -> yes (matches audit)."""
    s = sig.assign(side_rank=(sig.side != "yes").astype(int))
    s = s.sort_values(["ticker", "secs_left", "side_rank"], ascending=[True, False, True])
    return s.groupby("ticker", as_index=False).first()


def apply_cap_max1_no(sig, cap=2, max_no=1):
    """Codex's final locked rule: global cap 2, but at most ONE NO per close cluster.

    Vectorized: drop NO signals that are not the first NO in their cluster,
    then apply the ordinary global cap to what remains.
    """
    s = sig.sort_values(["close_ts", "secs_left", "series_rank"], ascending=[True, False, True])
    no_rank = s[s.side == "no"].groupby("close_ts").cumcount()
    drop = no_rank[no_rank >= max_no].index
    return apply_cap(s.drop(index=drop), cap=cap)


def metrics(sig, label):
    if sig.empty:
        return {"policy": label, "trades": 0}
    rows = sig.apply(price_signal, axis=1, result_type="expand")
    sig = sig.assign(contracts=rows[0], fee=rows[1], pnl=rows[2])
    per_cluster = sig.groupby("close_ts").pnl.sum()
    boot = [RNG.choice(per_cluster.values, len(per_cluster), replace=True).mean() for _ in range(5000)]
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {
        "policy": label,
        "trades": len(sig),
        "clusters": int(per_cluster.size),
        "win_rate": float(sig.won.mean()),
        "net_pnl": float(sig.pnl.sum()),
        "pnl_per_trade": float(sig.pnl.mean()),
        "pnl_per_cluster": float(per_cluster.mean()),
        "cluster_ci95": (float(lo), float(hi)),
        "_per_cluster": per_cluster,
    }


def main():
    d = pd.read_csv("/Users/chrisgarceau/pm/backtest_ablation_raw.csv")
    d = d.dropna(subset=["prior_1", "prior_2"])
    span = (pd.to_datetime(d.close_ts.min(), unit="s"), pd.to_datetime(d.close_ts.max(), unit="s"))
    days = (d.close_ts.max() - d.close_ts.min()) / 86400
    print(f"Window: {span[0]:%Y-%m-%d} -> {span[1]:%Y-%m-%d}  ({days:.1f} days)")
    print(f"Series: {', '.join(SERIES_ORDER)}   (WTI excluded - NO candidate never covered it)\n")

    sig_live = qualify(d, live_prior3_gate=True)
    sig_audit = qualify(d, live_prior3_gate=False)

    yes_live = apply_cap(sig_live[sig_live.side == "yes"])
    no_only = apply_cap(sig_audit[sig_audit.side == "no"])
    both = apply_cap(earliest_side(sig_audit))
    yes_audit = apply_cap(sig_audit[sig_audit.side == "yes"])

    both_max1 = apply_cap_max1_no(earliest_side(sig_audit))
    results = [
        metrics(yes_live, "CURRENT LIVE (YES only, prior3 gate, cap2)"),
        metrics(yes_audit, "YES only, no prior3 gate (audit baseline)"),
        metrics(no_only, "NO 90-91 only, cap2"),
        metrics(both, "EARLIEST SIDE (YES+NO share cap2)"),
        metrics(both_max1, "CODEX FINAL: earliest side, cap2, MAX 1 NO"),
    ]

    hdr = f"{'policy':<44}{'trades':>7}{'WR':>8}{'net P&L':>11}{'$/trade':>9}{'$/day':>9}"
    print(hdr); print("-" * len(hdr))
    for r in results:
        print(f"{r['policy']:<44}{r['trades']:>7}{r['win_rate']*100:>7.1f}%"
              f"{r['net_pnl']:>11,.0f}{r['pnl_per_trade']:>9.2f}{r['net_pnl']/days:>9.0f}")

    print("\nCluster-bootstrap 95% CI on $/cluster (clusters = simultaneous closes):")
    for r in results:
        lo, hi = r["cluster_ci95"]
        print(f"  {r['policy']:<44} {r['pnl_per_cluster']:>6.2f}  [{lo:>6.2f}, {hi:>6.2f}]")

    for idx, name in ((3, "earliest-side"), (4, "CODEX FINAL max-1-NO")):
        a = results[idx]["_per_cluster"].rename("both")
        b = results[0]["_per_cluster"].rename("live")
        j = pd.concat([a, b], axis=1).fillna(0.0)
        dl = (j["both"] - j["live"]).values
        bt = np.array([RNG.choice(dl, len(dl), replace=True).mean() for _ in range(20000)])
        print(f"\nPaired delta ({name} minus current live), {len(dl)} clusters:")
        print(f"  net delta        ${dl.sum():+,.0f}")
        print(f"  mean per cluster ${dl.mean():+.3f}")
        print(f"  P(delta > 0)      {(bt > 0).mean():.4f}")
        print(f"  95% CI           [{np.percentile(bt,2.5):+.3f}, {np.percentile(bt,97.5):+.3f}]")
        print(f"  one-sided 98.75% lower bound  ${np.percentile(bt,1.25):+.3f}  <-- gate needs > 0")

    a = results[3]["_per_cluster"].rename("both")
    b = results[0]["_per_cluster"].rename("live")
    j = pd.concat([a, b], axis=1).fillna(0.0)
    delta = (j["both"] - j["live"]).values
    boot = np.array([RNG.choice(delta, len(delta), replace=True).mean() for _ in range(2000)])
    print("\nNO 90-91 by series:")
    n = no_only.copy()
    r = n.apply(price_signal, axis=1, result_type="expand")
    n = n.assign(pnl=r[2])
    g = n.groupby("series").agg(trades=("pnl", "size"), wr=("won", "mean"), pnl=("pnl", "sum"))
    g["per_trade"] = g.pnl / g.trades
    for s, row in g.sort_values("pnl", ascending=False).iterrows():
        print(f"  {s:<12}{int(row.trades):>5} tr {row.wr*100:>6.1f}% WR  ${row.pnl:>8,.0f}  ${row.per_trade:>6.2f}/tr")

    print("\nNO 90-91 by ask:")
    for ask, row in n.groupby("ask").agg(trades=("pnl","size"), wr=("won","mean"), pnl=("pnl","sum")).iterrows():
        print(f"  {int(ask)}c {int(row.trades):>5} tr {row.wr*100:>6.1f}% WR  ${row.pnl:>8,.0f}  ${row.pnl/row.trades:>6.2f}/tr")

    print("\nNO 90-91 by half-month (is there a regime shift?):")
    n["period"] = pd.to_datetime(n.close_ts, unit="s").dt.to_period("D")
    n["bucket"] = pd.cut(n.close_ts, bins=4, labels=["Jun13-27", "Jun28-Jul12", "Jul13-28", "Jul29-Aug12"])
    for b, row in n.groupby("bucket", observed=True).agg(
            trades=("pnl", "size"), wr=("won", "mean"), pnl=("pnl", "sum")).iterrows():
        print(f"  {b:<14}{int(row.trades):>5} tr {row.wr*100:>6.1f}% WR  ${row.pnl:>8,.0f}  ${row.pnl/row.trades:>6.2f}/tr")

    print("\nLast 10 days of the window (closest to the forward holdout):")
    cut = d.close_ts.max() - 10 * 86400
    tail = n[n.close_ts >= cut]
    if len(tail):
        print(f"  NO 90-91: {len(tail)} tr {tail.won.mean()*100:.1f}% WR  "
              f"${tail.pnl.sum():,.0f}  ${tail.pnl.mean():.2f}/tr")


if __name__ == "__main__":
    main()
