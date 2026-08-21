#!/usr/bin/env python3
"""Are the bot's bad extra picks the adverse-momentum entries MOM3 detects?

Mechanism worth testing: the model takes the EARLIEST signal in a cluster — a market
that entered the 90-93c band and stayed there. The bot, scanning series in random
order, also picks up markets that dropped INTO the band later. A market falling into
the band is one whose ask is deteriorating, i.e. spot moving toward the strike.

That is precisely what m3 measures, and it is already shadow-logged live. If the
bot's losing extras carry adverse m3 while the shared entries do not, then MOM3 is
not a +12% garnish — it is a fix for the single largest leak in the audit.
"""
import os, re, sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "research", "perp_overlay"))
import backtest as B
import research as R          # spot cache + feature builder

cfg = B.live_config()
BET, SLIP = cfg["bet"], 0.105
DAY = "2026-08-19"

RE_TRADE = re.compile(
    r"TRADE:\s+(\S+)\s+(\d+)s left\s+(YES|NO)\s+scan=([\d.]+)c\s+fresh=([\d.]+)c")
live = {}
for ln in open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/a19.txt", errors="replace"):
    m = RE_TRADE.search(ln)
    if m:
        live[(m.group(1), m.group(3).lower())] = dict(
            secs=int(m.group(2)), fresh=float(m.group(5)))

rows = B.load(since=DAY, until=DAY)
won = {(tk, side): w for (se, tk, cts, side, ask, secs, w, p1, p2, p3) in rows}
clusters = defaultdict(list)
for r in rows:
    clusters[r[2]].append(r)
model = set()
for cts, crows in clusters.items():
    best = {}
    for (se, tk, _, side, ask, secs, w, p1, p2, p3) in crows:
        if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
            continue
        k = (tk, side)
        if k not in best or secs > best[k][5]:
            best[k] = (se, tk, cts, side, ask, secs, w, p1, p2, p3)
    for v in sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]:
        model.add((v[1], v[3]))

# m3 for every archived candidate on this day, keyed by (ticker, side)
cands = R.build()
m3_by = {}
for t in cands:
    if t["day"] != DAY or t["m3"] is None:
        continue
    k = (t["ticker"], t["side"])
    # the entry the bot/model would have used: earliest (highest secs) observation
    if k not in m3_by or t["secs"] > m3_by[k]["secs"]:
        m3_by[k] = t

shared = [k for k in live if k in model]
extra = [k for k in live if k not in model]
missed = [k for k in model if k not in live]


def summarise(label, ks):
    got = [(k, m3_by[k]) for k in ks if k in m3_by]
    if not got:
        print(f"  {label:<34} no m3 available")
        return
    vals = sorted(t["m3"] for _, t in got)
    adverse = sum(1 for v in vals if v > 0.5)
    pv = [B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP)
          if k in live else B.pnl(won[k], m3_by[k]["ask"], BET, SLIP) for k, _ in got]
    w = sum(1 for k, _ in got if won.get(k))
    print(f"  {label:<34} n={len(got):>3}  median m3 {vals[len(vals)//2]:>+6.2f}  "
          f"m3>+0.50 {adverse:>3} ({adverse/len(got)*100:>3.0f}%)  "
          f"WR {w/len(got)*100:>5.1f}%  ${sum(pv)/len(pv):>+6.2f}/tr")


print(f"m3 coverage: {len(m3_by)} of the day's archived (ticker,side) pairs\n")
print("=" * 84)
print("IS ADVERSE MOMENTUM WHAT SEPARATES THE BOT'S GOOD PICKS FROM ITS BAD ONES?")
print("=" * 84)
summarise("shared (model + bot agree)", shared)
summarise("bot-only extras (the losers)", extra)
summarise("model-only (bot missed these)", missed)

print("\n" + "=" * 84)
print("WOULD THE MOM3 VETO HAVE BLOCKED THE BAD ONES?")
print("=" * 84)
for thr in (1.0, 0.5, 0.25):
    blocked = [k for k in extra if k in m3_by and m3_by[k]["m3"] > thr]
    kept_sh = [k for k in shared if k in m3_by and m3_by[k]["m3"] > thr]
    if not blocked and not kept_sh:
        continue
    bv = sum(B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP) for k in blocked)
    sv = sum(B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP) for k in kept_sh)
    print(f"  m3 > {thr:+.2f}: blocks {len(blocked):>2} of {len(extra)} extras "
          f"(${bv:+,.2f}) and {len(kept_sh):>2} of {len(shared)} shared (${sv:+,.2f})"
          f"   net ${-(bv+sv):+,.2f}")
