#!/usr/bin/env python3
"""Corrected capture audit.

Fixes a real bug in the first pass: the modelled universe was every archived series,
including KXGOLD15M / KXSILVER15M / KXWTI15M, which are in SHADOW_SERIES and which the
bot deliberately does not trade. Counting those as "misses" inflated the miss count
and understated capture. The modelled set must be restricted to the trader's live
SERIES_LIST, read by AST so it cannot drift.

WTI is handled separately: it was paused mid-Aug-19 (PR #132), so for that day it is
neither cleanly live nor cleanly shadow. Reported as its own line.
"""
import ast, bisect, os, re, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET, SLIP = cfg["bet"], 0.105
DAY = sys.argv[2] if len(sys.argv) > 2 else "2026-08-19"

tree = ast.parse(open(os.path.join(ROOT, "late_certainty_trader.py")).read())
LIVE_SERIES = SHADOW = None
for n in tree.body:
    if isinstance(n, ast.Assign):
        for tg in n.targets:
            if isinstance(tg, ast.Name) and tg.id == "SERIES_LIST":
                LIVE_SERIES = set(ast.literal_eval(n.value))
            if isinstance(tg, ast.Name) and tg.id == "SHADOW_SERIES":
                SHADOW = set(ast.literal_eval(n.value))
print(f"live series ({len(LIVE_SERIES)}): {sorted(s.replace('KX','').replace('15M','') for s in LIVE_SERIES)}")

RE_TS = re.compile(r"(\d{4}-\d\d-\d\dT[\d:]+)\.")
RE_TRADE = re.compile(
    r"TRADE:\s+(\S+)\s+(\d+)s left\s+(YES|NO)\s+scan=([\d.]+)c\s+fresh=([\d.]+)c")
RE_SKIP = re.compile(r"SKIP\s+(\S+)\s+—\s+(.+?)\s*$")
RE_HALT = re.compile(r"HALTED\s+—")

live, skips, halts = {}, defaultdict(Counter), []
for ln in open(sys.argv[1], errors="replace"):
    mt = RE_TS.search(ln)
    if not mt:
        continue
    t = int(datetime.strptime(mt.group(1), "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=timezone.utc).timestamp())
    m = RE_TRADE.search(ln)
    if m:
        live[(m.group(1), m.group(3).lower())] = dict(
            t=t, secs=int(m.group(2)), fresh=float(m.group(5)))
        continue
    m = RE_SKIP.search(ln)
    if m:
        skips[m.group(1)][m.group(2)] += 1
        continue
    if RE_HALT.search(ln):
        halts.append(t)
halts.sort()

rows = [r for r in B.load(since=DAY, until=DAY)]
won = {(tk, s): w for (se, tk, c, s, a, sec, w, p1, p2, p3) in rows}
ser = lambda k: k[0].split("-")[0]


def build_model(series_filter):
    cl = defaultdict(list)
    for r in rows:
        if r[0] in series_filter:
            cl[r[2]].append(r)
    out, mrow = set(), {}
    for cts, cr in cl.items():
        best = {}
        for (se, tk, _, s, a, sec, w, p1, p2, p3) in cr:
            if not B.qualifies(cfg, se, s, a, sec, p1, p2, p3):
                continue
            k = (tk, s)
            if k not in best or sec > best[k][5]:
                best[k] = (se, tk, cts, s, a, sec, w, p1, p2, p3)
        for v in sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]:
            out.add((v[1], v[3]))
            mrow[(v[1], v[3])] = v
    return out, mrow


for label, filt in (("ALL archived series (the buggy first pass)",
                     LIVE_SERIES | SHADOW),
                    ("LIVE SERIES_LIST only (correct)", LIVE_SERIES),
                    ("LIVE + WTI (WTI paused mid-day)", LIVE_SERIES | {"KXWTI15M"})):
    model, mrow = build_model(filt)
    lv = {k for k in live if ser(k) in filt}
    both = model & lv
    print(f"\n{label}")
    print(f"  modelled {len(model):>4} | live {len(lv):>3} | both {len(both):>3} | "
          f"exact {len(both)/max(len(model),1)*100:>5.1f}% | "
          f"volume {len(lv)/max(len(model),1)*100:>5.1f}% | "
          f"bot-only {len(lv-model):>3}")

model, mrow = build_model(LIVE_SERIES)
lv = {k for k in live if ser(k) in LIVE_SERIES}
both, extra, missed = model & lv, lv - model, model - lv
pv = lambda ks: [B.pnl(won[k], round(live[k]["fresh"]) if k in live else mrow[k][4],
                       BET, SLIP) for k in ks if k in won]

print("\n" + "=" * 78)
print("CORRECTED PICTURE — live series only")
print("=" * 78)
for lab, ks in (("shared (model + bot)", both), ("bot-only extras", extra),
                ("model-only (missed)", missed)):
    v = pv(ks)
    w = sum(1 for k in ks if won.get(k))
    if v:
        print(f"  {lab:<24} n={len(ks):>3}  WR {w/len(v)*100:>5.1f}%  "
              f"${sum(v)/len(v):>+6.2f}/tr  ${sum(v):>+9.2f}")

print("\n  miss taxonomy")


def halted_at(t, tol=150):
    i = bisect.bisect_left(halts, t - tol)
    return i < len(halts) and halts[i] <= t + tol


cat, lost = Counter(), defaultdict(float)
for k in missed:
    v = mrow[k]
    if k[0] in skips:
        lab = "gate/skip: " + skips[k[0]].most_common(1)[0][0].split(":")[0][:30]
    elif halted_at(v[2] - v[5]):
        lab = "HALTED at entry time"
    else:
        lab = "NOT POLLED (genuine gap)"
    cat[lab] += 1
    lost[lab] += B.pnl(v[6], v[4], BET, SLIP)
for lab, n in cat.most_common():
    print(f"    {lab:<44} {n:>3} {n/len(missed)*100:>5.1f}%  ${lost[lab]:>+9.2f}")
