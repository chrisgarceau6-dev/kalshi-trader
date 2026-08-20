#!/usr/bin/env python3
"""H5/H6/H7 — every serious way to hold a perp against these positions.

A perp's expected return is zero, so no perp position can add EV; it can only
reshape variance and pay fees. The one route by which that could still be a win is
Kelly: if the hedge cuts variance enough, you can size UP at the same risk and
capture more of the 2pp edge. That is tested explicitly at the bottom.
"""
import math, statistics
from collections import defaultdict
import numpy as np
import research as R

cands = R.build()
cfg = R.B.live_config()
bet = cfg["bet"]
sel = R.select(cands, cfg)
sel = [t for t in sel if t["hedge"] is not None]
N = len(sel)
base_pnl = {id(t): R.pnl(t, bet) for t in sel}
BASE = sum(base_pnl.values())
phi = lambda z: math.exp(-z * z / 2) / math.sqrt(2 * math.pi)


def clusters_of(pairs):
    per = defaultdict(float)
    for t, v in pairs:
        per[t["cts"]] += v
    return per


def stats(pairs, label, extra=""):
    per = clusters_of(pairs)
    v = np.array(list(per.values()))
    eq = np.cumsum(v[np.argsort(list(per.keys()))])
    dd = float((eq - np.maximum.accumulate(eq)).min())
    tot = float(v.sum())
    sd = float(v.std())
    print(f"  {label:<34} {tot:>+9,.0f} {tot/N:>+7.2f} {sd:>8.2f} "
          f"{tot/N/sd*100 if sd else 0:>7.2f} {dd:>+9,.0f}  {extra}")
    return tot, sd


print("=" * 96)
print("H5 — STATIC HEDGE, sized per trade by the digital's actual delta")
print("=" * 96)
print("  delta of a digital = C*phi(z)/(sigma*sqrt(tau_eff)); a fixed notional is a")
print("  strawman, this is the textbook-correct hedge for each individual position.\n")
print(f"  {'variant':<34} {'total$':>9} {'$/tr':>7} {'clu sd':>8} {'ret/sd%':>7} {'maxDD':>9}")
stats([(t, base_pnl[id(t)]) for t in sel], "no hedge (baseline)")

for t in sel:
    C = bet / (t["ask"] / 100.0)
    denom = t["sigma"] * math.sqrt(t["tau_eff"] / 60.0)
    t["q_delta"] = C * phi(t["z"]) / denom if denom else 0.0

qs = [t["q_delta"] for t in sel]
print(f"\n  delta-implied notional: median ${statistics.median(qs):,.0f}, "
      f"mean ${statistics.mean(qs):,.0f}, "
      f"p90 ${sorted(qs)[int(.9*len(qs))]:,.0f}  (bet is ${bet:.0f})")
for bps in (0, 2, 5, 10):
    stats([(t, base_pnl[id(t)] + t["q_delta"] * t["hedge"] - t["q_delta"] * bps / 1e4)
           for t in sel], f"delta-hedged @ {bps}bp round trip")

print("\n  fixed-notional grid, for comparison")
for q in (500, 2000, 5000):
    for bps in (0, 5):
        stats([(t, base_pnl[id(t)] + q * t["hedge"] - q * bps / 1e4) for t in sel],
              f"fixed ${q:,} @ {bps}bp")

print("\n" + "=" * 96)
print("H6 — CLUSTER-NETTED HEDGE: one BTC perp per settlement cluster")
print("=" * 96)
print("  The 7 series settle together and all move with BTC, so the per-trade hedges")
print("  can be netted into a single BTC leg. If the two open positions point opposite")
print("  ways the legs cancel and the hedge is nearly free. Beta from 1-min returns.\n")

# beta of each series to BTC, from the entry-minute returns we already have
by_series = defaultdict(list)
for t in sel:
    by_series[t["series"]].append(t)
btc_sig = statistics.mean([t["sigma"] for t in sel if t["series"] == "KXBTC15M"])
beta = {}
for se, ss in by_series.items():
    beta[se] = statistics.mean([t["sigma"] for t in ss]) / btc_sig
print("  vol-ratio beta vs BTC: " + "  ".join(f"{s.replace('KX','').replace('15M',''):>5}"
                                              f"={b:.2f}" for s, b in sorted(beta.items())))

cl = defaultdict(list)
for t in sel:
    cl[t["cts"]].append(t)
netted, gross_legs, net_legs = [], 0.0, 0.0
for cts, ts in cl.items():
    # signed BTC-equivalent notional; sign convention already inside t["hedge"]
    signed = sum(-t["sign"] * t["q_delta"] * beta[t["series"]] for t in ts)
    gross_legs += sum(t["q_delta"] * beta[t["series"]] for t in ts)
    net_legs += abs(signed)
    btc_ret = statistics.mean([t["ret"] / beta[t["series"]] for t in ts])
    netted.append((cts, ts, signed, btc_ret))
print(f"  gross notional ${gross_legs:,.0f} -> netted ${net_legs:,.0f} "
      f"({(1-net_legs/gross_legs)*100:.1f}% cancels inside clusters)\n")
print(f"  {'variant':<34} {'total$':>9} {'$/tr':>7} {'clu sd':>8} {'ret/sd%':>7} {'maxDD':>9}")
for bps in (0, 2, 5, 10):
    pairs = []
    for cts, ts, signed, btc_ret in netted:
        hedge_pl = signed * btc_ret - abs(signed) * bps / 1e4
        for i, t in enumerate(ts):
            pairs.append((t, base_pnl[id(t)] + (hedge_pl if i == 0 else 0.0)))
    stats(pairs, f"cluster-netted BTC @ {bps}bp")

print("\n" + "=" * 96)
print("H7 — DYNAMIC: hedge only after spot crosses to the adverse side mid-position")
print("=" * 96)
print("  Fires on a minority of positions, so the fee load is far lower. Perp opened")
print("  at the first minute where z falls below the trigger, held to settlement.\n")
spot = R.load_spot()
print(f"  {'variant':<34} {'total$':>9} {'$/tr':>7} {'clu sd':>8} {'ret/sd%':>7} {'maxDD':>9}")
for trig in (0.0, 0.5):
    for q in (2000, 5000):
        for bps in (0, 5):
            pairs, fired = [], 0
            for t in sel:
                sm = spot[t["series"]]
                t_obs = int(t["cts"] - t["secs"])
                hp, done = 0.0, False
                m = t_obs
                while m < t["cts"] - 60 and not done:
                    px = sm.get(m)
                    if px:
                        tau = max(t["cts"] - m - 40, 30)
                        zz = (t["sign"] * math.log(px / t["strike"])
                              / (t["sigma"] * math.sqrt(tau / 60.0)))
                        if zz < trig:
                            hp = -t["sign"] * q * (t["ps"] - px) / px - q * bps / 1e4
                            fired += 1
                            done = True
                    m += 60
                pairs.append((t, base_pnl[id(t)] + hp))
            stats(pairs, f"trigger z<{trig} ${q:,} @{bps}bp",
                  f"fired {fired/N*100:.0f}% of positions")

print("\n" + "=" * 96)
print("KELLY — can a variance cut justify sizing UP?")
print("=" * 96)
print("  Kelly fraction f* ∝ μ/σ². A hedge that cuts σ by x lets you size up by")
print("  1/(1-x)², but only if μ survives the hedge's cost and drift bleed.")
print("  The scale-free comparison is return per unit of risk: μ/σ.\n")
b_tot, b_sd = stats([(t, base_pnl[id(t)]) for t in sel], "baseline")
for bps in (0, 5):
    h_tot, h_sd = stats([(t, base_pnl[id(t)] + t["q_delta"] * t["hedge"]
                          - t["q_delta"] * bps / 1e4) for t in sel],
                        f"delta-hedged @ {bps}bp")
    k = (b_sd / h_sd) ** 2 if h_sd else 0
    print(f"     -> sigma {(1-h_sd/b_sd)*100:+.1f}%, Kelly size x{k:.2f}, "
          f"EV at that size ${h_tot/N*k:+.2f}/tr vs baseline ${b_tot/N:+.2f}/tr")
