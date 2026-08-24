#!/usr/bin/env python3
"""Two corrections the first pass needs before its number means anything.

1. "NEVER POLLED" conflates two opposite conclusions. When the trader is halted it
   returns BEFORE scanning, so no SKIP line is written for any ticker — a halted
   miss looks identical to an unpolled one. One is fixed already (#134 restored the
   $300 floor); the other needs a WebSocket. Split them using the HALTED timestamps.

2. Exact (ticker, side) matching understates capture. The model fills its 2 slots by
   highest secs_left; the live bot scans series in random order and fills them with
   whatever it meets first. A different-but-equivalent entry in the same cluster is a
   SUBSTITUTION, not a miss. Report volume capture and cluster capture alongside it.
"""
import os, re, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET = cfg["bet"]
DAY = "2026-08-19"

RE_TS = re.compile(r"(\d{4}-\d\d-\d\dT[\d:]+)\.")
RE_TRADE = re.compile(r"TRADE:\s+(\S+)\s+(\d+)s left\s+(YES|NO)")
RE_SKIP = re.compile(r"SKIP\s+(\S+)\s+—\s+(.+?)\s*$")
RE_HALT = re.compile(r"HALTED\s+—\s+(.+?)\s*$")


def ts_of(ln):
    m = RE_TS.search(ln)
    if not m:
        return None
    return int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp())


live, skips, halt_ts, poll_ts = {}, defaultdict(Counter), [], []
halt_reasons = Counter()
for ln in open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/a19.txt", errors="replace"):
    t = ts_of(ln)
    if t is None:
        continue
    m = RE_TRADE.search(ln)
    if m:
        live[(m.group(1), m.group(3).lower())] = t
        poll_ts.append(t)
        continue
    m = RE_SKIP.search(ln)
    if m:
        skips[m.group(1)][m.group(2)] += 1
        poll_ts.append(t)
        continue
    m = RE_HALT.search(ln)
    if m:
        halt_ts.append(t)
        halt_reasons[m.group(1)[:60]] += 1

halt_ts.sort()
poll_ts.sort()
import bisect


def halted_at(t, tol=150):
    i = bisect.bisect_left(halt_ts, t - tol)
    return i < len(halt_ts) and halt_ts[i] <= t + tol


rows = B.load(since=DAY, until=DAY)
clusters = defaultdict(list)
for r in rows:
    clusters[r[2]].append(r)
modelled, model_rows = set(), {}
by_cluster = defaultdict(list)
for cts, crows in clusters.items():
    best = {}
    for (se, tk, _, side, ask, secs, won, p1, p2, p3) in crows:
        if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
            continue
        k = (tk, side)
        if k not in best or secs > best[k][5]:
            best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
    for v in sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]:
        modelled.add((v[1], v[3]))
        model_rows[(v[1], v[3])] = v
        by_cluster[cts].append((v[1], v[3]))

pnl = lambda k: B.pnl(model_rows[k][6], model_rows[k][4], BET, 0.105)
both = modelled & set(live)
print(f"HALT COVERAGE: {len(halt_ts)} halted polls, "
      f"{len(poll_ts)} active polls  →  {len(halt_ts)/(len(halt_ts)+len(poll_ts))*100:.0f}% "
      f"of logged polls were during a halt")
for r, n in halt_reasons.most_common(3):
    print(f"    {n:>5}x  {r}")

print(f"\nCAPTURE, three ways")
print(f"  exact (ticker,side) match : {len(both)}/{len(modelled)} = "
      f"{len(both)/len(modelled)*100:.1f}%")
print(f"  volume (any qualifying)   : {len(live)}/{len(modelled)} = "
      f"{len(live)/len(modelled)*100:.1f}%")
cl_model = {c for c in by_cluster}
cl_live = {model_rows[k][2] for k in both}
print(f"  clusters participated in  : {len(cl_live)}/{len(cl_model)} = "
      f"{len(cl_live)/len(cl_model)*100:.1f}%")
print(f"  live-only entries (substitutions the model didn't pick): "
      f"{len(set(live) - modelled)}")

missed = modelled - set(live)
print(f"\nMISS TAXONOMY, halts separated out  (n={len(missed)})")
cat, lost = Counter(), defaultdict(float)
for k in missed:
    v = model_rows[k]
    entry_t = v[2] - v[5]
    if k[0] in skips:
        lab = "gate/skip: " + skips[k[0]].most_common(1)[0][0][:34]
    elif halted_at(entry_t):
        lab = "HALTED at entry time"
    else:
        lab = "NOT POLLED (genuine polling gap)"
    cat[lab] += 1
    lost[lab] += pnl(k)
print(f"  {'reason':<50} {'n':>4} {'%':>6} {'modelled P&L':>13}")
for lab, n in cat.most_common():
    print(f"  {lab:<50} {n:>4} {n/len(missed)*100:>5.1f}% {lost[lab]:>+13,.2f}")

fixed = lost.get("HALTED at entry time", 0.0)
gap = lost.get("NOT POLLED (genuine polling gap)", 0.0)
print(f"\n  captured                          ${sum(pnl(k) for k in both):+,.2f}")
print(f"  lost to halts (already fixed #134) ${fixed:+,.2f}")
print(f"  lost to the polling gap            ${gap:+,.2f}   <- the WebSocket prize")
print(f"  lost to gates / races              "
      f"${sum(v for k, v in lost.items() if k.startswith('gate')):+,.2f}")
