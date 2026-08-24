#!/usr/bin/env python3
"""Why did the bot's own picks lose money? Two hypotheses, and they are separable.

  H-SLOT   The bot took entries that WERE in the model's candidate pool but lost the
           MAX_CONCURRENT slot race. Pure allocation — same universe, worse ordering.
  H-INTRA  The bot took entries that NEVER qualified on candle data at all: signals
           that existed only between 1-minute closes. A different universe, and if
           those are adversely selected then the candle grid is a filter, not a
           lossy sample — which would invert the case for a WebSocket.

Separator: for each live-only entry, does the archive contain a candle for that
(ticker, side) that passes qualifies()? If yes -> H-SLOT. If no -> H-INTRA.

Also tests whether the halt caused it, by measuring time since the last halt.
"""
import bisect, os, re, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET, SLIP = cfg["bet"], 0.105
DAY = "2026-08-19"

RE_TS = re.compile(r"(\d{4}-\d\d-\d\dT[\d:]+)\.")
RE_TRADE = re.compile(
    r"TRADE:\s+(\S+)\s+(\d+)s left\s+(YES|NO)\s+scan=([\d.]+)c\s+fresh=([\d.]+)c")
RE_HALT = re.compile(r"HALTED\s+—")

ts_of = lambda ln: (int(datetime.strptime(RE_TS.search(ln).group(1), "%Y-%m-%dT%H:%M:%S")
                        .replace(tzinfo=timezone.utc).timestamp())
                    if RE_TS.search(ln) else None)

live, halts = {}, []
for ln in open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/a19.txt", errors="replace"):
    t = ts_of(ln)
    if t is None:
        continue
    m = RE_TRADE.search(ln)
    if m:
        live[(m.group(1), m.group(3).lower())] = dict(
            t=t, secs=int(m.group(2)), scan=float(m.group(4)), fresh=float(m.group(5)))
    elif RE_HALT.search(ln):
        halts.append(t)
halts.sort()

rows = B.load(since=DAY, until=DAY)
# every archive row, and every row that passes the gates
arch = defaultdict(list)
qual = defaultdict(list)
won = {}
for (se, tk, cts, side, ask, secs, w, p1, p2, p3) in rows:
    k = (tk, side)
    arch[k].append((ask, secs))
    won[k] = w
    if B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
        qual[k].append((ask, secs))

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

only_live = [k for k in live if k not in model]
print(f"{len(only_live)} live-only entries\n")
print("=" * 78)
print("H-SLOT vs H-INTRA")
print("=" * 78)
groups = defaultdict(list)
for k in only_live:
    if k not in arch:
        groups["not in archive at all"].append(k)
    elif k in qual:
        groups["H-SLOT: qualified on candles, lost the slot race"].append(k)
    else:
        groups["H-INTRA: never qualified on candle data"].append(k)

print(f"  {'group':<48} {'n':>4} {'WR':>7} {'$/tr':>8} {'total':>9}")
for g, ks in sorted(groups.items(), key=lambda x: -len(x[1])):
    pv = [B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP) for k in ks if k in won]
    w = sum(1 for k in ks if won.get(k))
    if pv:
        print(f"  {g:<48} {len(ks):>4} {w/len(pv)*100:>6.1f}% "
              f"{sum(pv)/len(pv):>+8.2f} {sum(pv):>+9,.2f}")
    else:
        print(f"  {g:<48} {len(ks):>4} {'—':>7} {'—':>8} {'—':>9}")

print("\n  for reference, entries BOTH took:")
bothk = [k for k in live if k in model]
pv = [B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP) for k in bothk if k in won]
w = sum(1 for k in bothk if won.get(k))
print(f"  {'shared entries':<48} {len(bothk):>4} {w/len(pv)*100:>6.1f}% "
      f"{sum(pv)/len(pv):>+8.2f} {sum(pv):>+9,.2f}")

print("\n" + "=" * 78)
print("WHY DID THEY NOT QUALIFY? compare the bot's ask to the candle record")
print("=" * 78)
intra = groups.get("H-INTRA: never qualified on candle data", [])
for k in intra[:12]:
    a = arch.get(k, [])
    inband = [x for x in a if 90 <= x[0] <= 93]
    print(f"  {k[0][:30]:<31} {k[1]:<4} bot entered {live[k]['fresh']:>5.1f}c "
          f"@{live[k]['secs']:>4}s | archive rows {len(a):>2}, "
          f"in 90-93c {len(inband):>2}, won={won.get(k)}")

print("\n" + "=" * 78)
print("DID THE HALT CAUSE IT? seconds since the last halt at entry")
print("=" * 78)


def since_halt(t):
    i = bisect.bisect_right(halts, t)
    return t - halts[i - 1] if i else None


for label, ks in (("live-only", only_live), ("shared", bothk)):
    d = [since_halt(live[k]["t"]) for k in ks]
    d = [x for x in d if x is not None]
    if not d:
        continue
    d.sort()
    close = sum(1 for x in d if x <= 300)
    pv = [B.pnl(won[k], round(live[k]["fresh"]), BET, SLIP)
          for k in ks if k in won and since_halt(live[k]["t"]) is not None
          and since_halt(live[k]["t"]) <= 300]
    print(f"  {label:<12} n={len(d):>3}  median {d[len(d)//2]:>6}s since a halt  "
          f"within 5min of a halt: {close:>3} ({close/len(d)*100:.0f}%)"
          + (f"  those worth ${sum(pv):+,.2f}" if pv else ""))
