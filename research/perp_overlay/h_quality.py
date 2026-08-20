#!/usr/bin/env python3
"""Data quality + does the baseline survive the spot join? Run before any hypothesis."""
import sys
from collections import defaultdict
import research as R

cands = R.build()
cfg = R.live_config = R.B.live_config()
sel = R.select(cands, cfg)
bet = cfg["bet"]

print(f"live cfg {cfg['version']}  bet ${bet:.0f}")
print(f"\ncandidates joined to spot: {len(cands):,}")
print(f"selected entries (live gates + max_conc): {len(sel):,}")

# how does that compare to the unjoined harness?
rows = R.B.load()
pc, tr = R.B.simulate(rows, cfg, 0.0)
print(f"harness on full archive (no spot join): {len(tr):,} trades, "
      f"${sum(pc.values()):+,.0f}")
p = [R.pnl(t, bet) for t in sel]
wr = sum(1 for t in sel if t["won"]) / len(sel) * 100
print(f"joined subset:                          {len(sel):,} trades, ${sum(p):+,.0f}"
      f"  {wr:.2f}%WR  {sum(p)/len(p):+.2f}/tr")
print("  -> the join drops WTI/GOLD/SILVER (no crypto product) and minutes where "
      "Coinbase printed no trade")

print("\nRETENTION AND BASELINE BY SERIES")
print(f"  {'series':<11} {'sel':>6} {'WR%':>7} {'$/tr':>7} {'total':>9} {'sigma bp':>9}")
for se in sorted({t["series"] for t in sel}):
    ss = [t for t in sel if t["series"] == se]
    pl = [R.pnl(t, bet) for t in ss]
    sg = sum(t["sigma"] for t in ss) / len(ss) * 1e4
    print(f"  {se:<11} {len(ss):>6} "
          f"{sum(1 for t in ss if t['won'])/len(ss)*100:>7.2f} "
          f"{sum(pl)/len(pl):>+7.2f} {sum(pl):>+9,.0f} {sg:>9.2f}")

print("\nIS / OOS SPLIT")
for tag, ss in (("IS ", R.split(sel)[0]), ("OOS", R.split(sel)[1])):
    pl = [R.pnl(t, bet) for t in ss]
    print(f"  {tag} n={len(ss):>5} clusters={len({t['cts'] for t in ss}):>5} "
          f"WR {sum(1 for t in ss if t['won'])/len(ss)*100:.2f}%  "
          f"${sum(pl):+,.0f}  {sum(pl)/len(pl):+.2f}/tr")

print("\nIS THE MODEL CALIBRATED AT ALL? (z -> realised WR vs Phi(z))")
bs = R.buckets(sel, lambda t: t["z"], 8)
print(f"  {'z range':>18} {'n':>6} {'WR%':>7} {'Phi(z)%':>8} {'ask':>6}")
for lo, hi, ch in bs:
    wr = sum(1 for t in ch if t["won"]) / len(ch) * 100
    pm = sum(t["p_model"] for t in ch) / len(ch) * 100
    ak = sum(t["ask"] for t in ch) / len(ch)
    print(f"  [{lo:>7.2f},{hi:>7.2f}] {len(ch):>6} {wr:>7.2f} {pm:>8.2f} {ak:>6.2f}")
