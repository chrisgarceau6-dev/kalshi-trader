#!/usr/bin/env python3
"""If we see 90c with 10 minutes left, what is that same contract worth at 3 minutes,
and did waiting pay?

The archive only keeps asks inside [88,96], so a contract that has left that window
has no later row. That censoring is exactly what a waiting trader experiences: it
either drifted up out of reach, or it crashed and they would not have bought it.
"""
import csv, gzip, statistics
from collections import defaultdict
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parent.parent / "data" / "candles"

# (ticker, side) -> {secs_left: ask}, plus the outcome
path = defaultdict(dict)
won = {}
for p in sorted(ARCHIVE.glob("*.csv.gz")):
    for r in csv.DictReader(gzip.open(p, "rt")):
        try:
            key = (r["ticker"], r["side"])
            path[key][int(r["secs_left"])] = float(r["ask"])
            won[key] = r["won"] in ("True", "true", "1")
        except (TypeError, ValueError):
            continue


def ask_in(key, lo, hi):
    """The ask observed in a time window, nearest the middle of it."""
    hits = [(s, a) for s, a in path[key].items() if lo <= s < hi]
    if not hits:
        return None
    mid = (lo + hi) / 2
    return min(hits, key=lambda t: abs(t[0] - mid))[1]


EARLY = (480, 600)     # ~8-10 minutes left
LATE = (150, 240)      # ~3-4 minutes left

for entry_lo, entry_hi, label in [(90, 91, "90-91c"), (92, 93, "92-93c")]:
    seen = [k for k in path
            if (a := ask_in(k, *EARLY)) is not None and entry_lo <= a <= entry_hi]
    if not seen:
        continue
    later = {k: ask_in(k, *LATE) for k in seen}
    still = {k: v for k, v in later.items() if v is not None}
    gone = len(seen) - len(still)
    up = sum(1 for v in still.values() if v >= 94)
    same = sum(1 for v in still.values() if 90 <= v < 94)
    down = sum(1 for v in still.values() if v < 90)

    early_wr = sum(won[k] for k in seen) / len(seen) * 100
    early_price = statistics.mean(ask_in(k, *EARLY) for k in seen)

    print(f"\n=== bought at {label} with 8-10 min left  (n={len(seen):,}) ===")
    print(f"  outcome if held        : {early_wr:.2f}% win   "
          f"(paid ~{early_price:.1f}c, edge {early_wr - early_price:+.2f}pp)")
    print(f"  where it was at 3-4 min:")
    print(f"     >= 94c              : {up:>6,}  ({up/len(seen)*100:>5.1f}%)")
    print(f"     90-93c              : {same:>6,}  ({same/len(seen)*100:>5.1f}%)")
    print(f"     88-89c              : {down:>6,}  ({down/len(seen)*100:>5.1f}%)")
    print(f"     left the 88-96 band : {gone:>6,}  ({gone/len(seen)*100:>5.1f}%)")

    # What the waiter gets: only buys what is still in band at 3-4 minutes.
    for lo, hi, name in [(90, 93, "waits, buys 90-93c"), (94, 96, "waits, buys 94-96c")]:
        sub = [k for k, v in still.items() if lo <= v <= hi]
        if len(sub) < 100:
            continue
        wr = sum(won[k] for k in sub) / len(sub) * 100
        price = statistics.mean(still[k] for k in sub)
        # per $75 staked, ignoring fees, for comparability with the early entry
        ev = (wr / 100 * (100 - price) - (1 - wr / 100) * price) / price * 75
        print(f"  {name:<22}: {len(sub):>6,} trades  {wr:.2f}% win at ~{price:.1f}c  "
              f"edge {wr - price:+.2f}pp   EV ${ev:+.2f}/trade")
    ev_early = (early_wr / 100 * (100 - early_price)
                - (1 - early_wr / 100) * early_price) / early_price * 75
    print(f"  {'buys early instead':<22}: {len(seen):>6,} trades  {early_wr:.2f}% win at "
          f"~{early_price:.1f}c  edge {early_wr - early_price:+.2f}pp   EV ${ev_early:+.2f}/trade")
