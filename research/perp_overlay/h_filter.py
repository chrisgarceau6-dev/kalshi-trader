#!/usr/bin/env python3
"""Is H2 (adverse spot momentum) actually ACTIONABLE?

A signal that predicts is not the same as a filter that pays. This strategy is a
volume business: §7 records a filter that improved per-trade stats while cutting
holdout P&L from +$4,242 to +$992. So every test here reports TOTAL P&L, both windows.

Three ways to use a signal without throwing away volume:
  (a) veto  — refuse entries above a momentum threshold (costs volume)
  (b) rank  — MAX_CONCURRENT=2 forces a choice among candidates; rank by signal
              instead of by secs_left (costs nothing)
  (c) widen — use the signal to safely take entries currently excluded (adds volume)
"""
import research as R

cands = R.build()
cfg = R.B.live_config()
bet = cfg["bet"]
sel = R.select(cands, cfg)


def line(tag, ss):
    if not ss:
        return f"  {tag:<26} —"
    pl = [R.pnl(t, bet) for t in ss]
    wr = sum(1 for t in ss if t["won"]) / len(ss) * 100
    return (f"  {tag:<26} n={len(ss):>5}  WR {wr:>6.2f}%  "
            f"{sum(pl)/len(pl):>+6.2f}/tr  total {sum(pl):>+8,.0f}")


print("=" * 78)
print("(a) VETO — block entries whose adverse 3-min momentum exceeds a threshold")
print("=" * 78)
for tag, ss in (("IN-SAMPLE", R.split(sel)[0]), ("HOLDOUT", R.split(sel)[1])):
    print(f"\n{tag}")
    have = [t for t in ss if t["m3"] is not None]
    print(line("no filter", have))
    for thr in (2.0, 1.5, 1.0, 0.75, 0.5, 0.25, 0.0, -0.25):
        print(line(f"block m3 > {thr:+.2f}", [t for t in have if t["m3"] <= thr]))

print("\n" + "=" * 78)
print("(b) RANK — same trade count, but pick which candidates get the 2 slots")
print("=" * 78)
alt = R.select(cands, cfg, rank="z")
bym = R.select(cands, cfg, rank="secs")   # baseline


def rank_by(cands, cfg, keyf):
    from collections import defaultdict
    clusters = defaultdict(list)
    for r in cands:
        clusters[r["cts"]].append(r)
    out = []
    for cts, crows in clusters.items():
        best = {}
        for r in crows:
            if not R.B.qualifies(cfg, r["series"], r["side"], r["ask"], r["secs"],
                                 r["p1"], r["p2"], r["p3"]):
                continue
            k = (r["ticker"], r["side"])
            if k not in best or r["secs"] > best[k]["secs"]:
                best[k] = r
        pool = [r for r in best.values() if keyf(r) is not None]
        pool += [r for r in best.values() if keyf(r) is None]
        out += sorted(pool, key=lambda r: (keyf(r) is None, keyf(r) or 0))[:cfg["max_conc"]]
    return out


rank_m3 = rank_by(cands, cfg, lambda r: r["m3"])          # least adverse first
rank_negz = rank_by(cands, cfg, lambda r: -r["z"])        # highest z first
for tag, i in (("IN-SAMPLE", 0), ("HOLDOUT", 1)):
    print(f"\n{tag}")
    print(line("rank by secs_left (live)", R.split(bym)[i]))
    print(line("rank by m3 (least adverse)", R.split(rank_m3)[i]))
    print(line("rank by z (highest)", R.split(rank_negz)[i]))

print("\n" + "=" * 78)
print("(c) WIDEN — does momentum make currently-excluded entries safe? (H8)")
print("=" * 78)
for lo, hi in ((94, 96), (88, 89)):
    w = dict(cfg, min_ask=lo, max_ask=hi)
    ws = R.select(cands, w)
    print(f"\nask {lo}-{hi}c  (currently excluded)")
    for tag, i in (("  IS ", 0), ("  OOS", 1)):
        ss = R.split(ws)[i]
        have = [t for t in ss if t["m3"] is not None]
        print(f"{tag}", line("all", have)[2:])
        print(f"{tag}", line("m3 <= 0 only", [t for t in have if t["m3"] <= 0])[2:])

print("\n" + "=" * 78)
print("(d) H10 — does momentum dominate the prior-candle gate?")
print("=" * 78)
nogate = dict(cfg, prior_min=0, p3_gate=0)
ng = R.select(cands, nogate)
for tag, i in (("IN-SAMPLE", 0), ("HOLDOUT", 1)):
    print(f"\n{tag}")
    print(line("live gates", R.split(sel)[i]))
    ss = [t for t in R.split(ng)[i] if t["m3"] is not None]
    print(line("no prior gate", ss))
    print(line("no prior gate, m3<=0", [t for t in ss if t["m3"] <= 0]))
