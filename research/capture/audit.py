#!/usr/bin/env python3
"""Capture-rate audit: what fraction of modelled entries did the bot actually take,
and for every miss, why?

Modelled set = archive rows passing the live gates, one entry per (ticker, side),
capped at MAX_CONCURRENT per cluster — exactly what scripts/backtest.py trades.
Live set = TRADE: lines from the Actions logs.

Misses are classified from the trader's own SKIP lines. A modelled entry with no
SKIP line for its ticker was never rejected — it was never seen, which is the
polling gap of Invariant 6 and the one failure mode a config change cannot fix.

Usage: python3 audit.py /tmp/cap_lines.txt 2026-08-18 2026-08-19
"""
import os, re, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET = cfg["bet"]

RE_TS = re.compile(r"(\d{4}-\d\d-\d\d)T[\d:.]+Z")
RE_TRADE = re.compile(r"TRADE:\s+(\S+)\s+(\d+)s left\s+(YES|NO)\s+scan=([\d.]+)c")
RE_SKIP = re.compile(r"SKIP\s+(\S+)\s+—\s+(.+?)\s*$")
RE_HALT = re.compile(r"HALTED\s+—\s+(.+?)\s*$")

BUCKETS = [
    ("live position/order exists", "duplicate — position already open"),
    ("heat check", "concurrency slot (MAX_CONCURRENT)"),
    ("thin book", "book depth gate"),
    ("last look", "book moved outside band before order"),
    ("could not refetch", "quote refetch failed"),
    ("could not fetch prior candles", "prior-candle fetch failed"),
    ("crashed to", "ask crashed between scan and order"),
    ("jumped to", "ask jumped between scan and order"),
    ("needs 3rd prior", "p3 gate"),
    ("prior ", "prior-candle gate"),
]


def classify(reason):
    for key, label in BUCKETS:
        if key in reason:
            return label
    return f"other: {reason[:40]}"


def main(path, days):
    live, skips, halts = set(), defaultdict(Counter), Counter()
    live_by_day = defaultdict(set)
    seen_tickers = defaultdict(set)
    for ln in open(path, errors="replace"):
        mts = RE_TS.search(ln)
        day = mts.group(1) if mts else "?"
        if day not in days:
            continue
        m = RE_TRADE.search(ln)
        if m:
            live.add((m.group(1), m.group(3).lower()))
            live_by_day[day].add((m.group(1), m.group(3).lower()))
            seen_tickers[day].add(m.group(1))
            continue
        m = RE_SKIP.search(ln)
        if m:
            skips[m.group(1)][classify(m.group(2))] += 1
            seen_tickers[day].add(m.group(1))
            continue
        m = RE_HALT.search(ln)
        if m:
            halts[day] += 1

    rows = B.load(since=min(days), until=max(days))
    clusters = defaultdict(list)
    for r in rows:
        clusters[r[2]].append(r)
    modelled = defaultdict(set)
    model_rows = {}
    for cts, crows in clusters.items():
        best = {}
        for (se, tk, _, side, ask, secs, won, p1, p2, p3) in crows:
            if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
                continue
            k = (tk, side)
            if k not in best or secs > best[k][5]:
                best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
        for v in sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]:
            d = datetime.fromtimestamp(cts, timezone.utc).strftime("%Y-%m-%d")
            modelled[d].add((v[1], v[3]))
            model_rows[(v[1], v[3])] = v

    print(f"{'day':<12} {'modelled':>9} {'live':>6} {'both':>6} {'capture':>8} "
          f"{'live-only':>10} {'halts':>6}")
    tot_m = tot_b = 0
    all_missed = []
    for d in sorted(days):
        m, l = modelled[d], live_by_day[d]
        both = m & l
        tot_m += len(m); tot_b += len(both)
        all_missed += [(d, x) for x in (m - l)]
        cap = len(both) / len(m) * 100 if m else 0
        print(f"{d:<12} {len(m):>9} {len(l):>6} {len(both):>6} {cap:>7.1f}% "
              f"{len(l - m):>10} {halts[d]:>6}")
    print(f"{'TOTAL':<12} {tot_m:>9} {'':>6} {tot_b:>6} "
          f"{tot_b/tot_m*100 if tot_m else 0:>7.1f}%")

    print(f"\nWHY THE {len(all_missed)} MISSES HAPPENED")
    reasons = Counter()
    lost = defaultdict(float)
    for d, k in all_missed:
        v = model_rows[k]
        pl = B.pnl(v[6], v[4], BET, 0.105)
        if k[0] in skips:
            lab = skips[k[0]].most_common(1)[0][0]
        elif k[0] in seen_tickers[d]:
            lab = "seen but no skip logged"
        else:
            lab = "NEVER POLLED — ticker absent from all logs"
        reasons[lab] += 1
        lost[lab] += pl
    print(f"  {'reason':<46} {'n':>5} {'%':>6} {'P&L not taken':>14}")
    for lab, n in reasons.most_common():
        print(f"  {lab:<46} {n:>5} {n/len(all_missed)*100:>5.1f}% {lost[lab]:>+14,.2f}")
    print(f"  {'':<46} {'':>5} {'':>6} {sum(lost.values()):>+14,.2f}")

    taken_pl = sum(B.pnl(model_rows[k][6], model_rows[k][4], BET, 0.105)
                   for d in days for k in (modelled[d] & live_by_day[d]))
    print(f"\n  modelled P&L on entries actually taken : ${taken_pl:+,.2f}")
    print(f"  modelled P&L left on the table          : ${sum(lost.values()):+,.2f}")


if __name__ == "__main__":
    main(sys.argv[1], set(sys.argv[2:]))
