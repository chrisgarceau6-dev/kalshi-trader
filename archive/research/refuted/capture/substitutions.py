#!/usr/bin/env python3
"""Are the bot's slot choices worse than the model's, or just different?

The model fills its 2 concurrency slots with the highest-secs_left candidates. The
live bot scans series in random order and fills them with whatever it meets first.
So inside a cluster both take 2 entries, but not the SAME 2. Exact-match capture
counts that as a miss plus an unexplained extra; economically it is a substitution.

This prices them: within clusters the bot actually participated in, compare what it
took against what the model would have taken. Everything valued at the current $50
so the comparison is forward-looking.
"""
import os, re, sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

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
            secs=int(m.group(2)), scan=float(m.group(4)), fresh=float(m.group(5)))

# outcome lookup for every (ticker, side) the archive knows about
rows = B.load(since=DAY, until=DAY)
won = {}
cts_of = {}
for (se, tk, cts, side, ask, secs, w, p1, p2, p3) in rows:
    won[(tk, side)] = w
    cts_of[(tk, side)] = cts

clusters = defaultdict(list)
for r in rows:
    clusters[r[2]].append(r)
model = defaultdict(list)
model_row = {}
for cts, crows in clusters.items():
    best = {}
    for (se, tk, _, side, ask, secs, w, p1, p2, p3) in crows:
        if not B.qualifies(cfg, se, side, ask, secs, w and 0 or 0, p2, p3) and False:
            pass
        if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
            continue
        k = (tk, side)
        if k not in best or secs > best[k][5]:
            best[k] = (se, tk, cts, side, ask, secs, w, p1, p2, p3)
    for v in sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]:
        model[cts].append((v[1], v[3]))
        model_row[(v[1], v[3])] = v

M = {k for v in model.values() for k in v}
L = set(live)
both, only_live, only_model = M & L, L - M, M - L
val_live = lambda k: (B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP)
                      if k in won else None)
val_model = lambda k: B.pnl(model_row[k][6], model_row[k][4], BET, SLIP)

print(f"model {len(M)} | live {len(L)} | both {len(both)} | "
      f"live-only {len(only_live)} | model-only {len(only_model)}\n")

print("THE 31 LIVE-ONLY ENTRIES — what the bot took that the model did not")
priced = [(k, val_live(k)) for k in only_live if val_live(k) is not None]
unpriced = len(only_live) - len(priced)
wins = sum(1 for k, _ in priced if won[k])
print(f"  priced from archive: {len(priced)}/{len(only_live)} "
      f"({unpriced} not in the 88-96c archive band)")
if priced:
    tot = sum(v for _, v in priced)
    print(f"  outcome: {wins}W / {len(priced)-wins}L = "
          f"{wins/len(priced)*100:.1f}% WR   total ${tot:+,.2f}  "
          f"(${tot/len(priced):+.2f}/trade)")

print("\nHEAD TO HEAD inside clusters the bot participated in")
part = {cts_of[k] for k in both | only_live if k in cts_of}
bot_v = mod_v = 0.0
bot_n = mod_n = 0
for c in part:
    mk = [k for k in model.get(c, [])]
    lk = [k for k in (both | only_live) if cts_of.get(k) == c]
    for k in mk:
        mod_v += val_model(k); mod_n += 1
    for k in lk:
        v = val_live(k)
        if v is not None:
            bot_v += v; bot_n += 1
print(f"  clusters: {len(part)}")
print(f"  model would have taken {mod_n} entries worth ${mod_v:+,.2f} "
      f"(${mod_v/max(mod_n,1):+.2f}/tr)")
print(f"  bot actually took      {bot_n} entries worth ${bot_v:+,.2f} "
      f"(${bot_v/max(bot_n,1):+.2f}/tr)")
print(f"  difference: ${bot_v-mod_v:+,.2f}")

print("\nWHY THEY DIFFER — secs_left at entry")
if priced:
    import statistics
    ls = [live[k]["secs"] for k, _ in priced]
    ms = [model_row[k][5] for k in only_model]
    print(f"  live-only entries : median {statistics.median(ls):.0f}s "
          f"(range {min(ls)}-{max(ls)})")
    if ms:
        print(f"  model-only misses : median {statistics.median(ms):.0f}s "
              f"(range {min(ms):.0f}-{max(ms):.0f})")
    print("  the model front-loads the earliest signals; the bot takes what it meets")
