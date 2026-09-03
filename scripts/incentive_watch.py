#!/usr/bin/env python3
"""Do the series we trade have a Kalshi incentive pool — now, or ever?

Kalshi runs two self-serve reward programs a retail algo account is eligible for.
The Market Maker tier needs a signed agreement we do not have and is excluded.

  VOLUME    pays pro-rata on MATCHED VOLUME, both sides, so our taker flow counts
            with NO strategy change. Capped $0.005/contract/account. Eligible
            volume is central-order-book fills priced 3c-97c — our 90-93c band
            sits inside it. This is the one that is free money if it returns.
  LIQUIDITY pays for RESTING SIZE near a reference price, scored on 1-second
            snapshots. We are 100% taker, so we earn exactly $0 from these no
            matter how many run on our series. Reported only so a live pool is
            never mistaken for money we are collecting.

Measured 2026-09-01:
  - KXBTC15M and KXETH15M each ran 306 VOLUME programs at $20/market, but only
    2026-05-09 -> 2026-05-12. A ~3-day pilot, long over. $6,120 per series.
    We would have qualified automatically. Nobody was watching, so we cannot say
    whether we collected — that is what this script now prevents.
  - Nothing live on any of our six today. The only live crypto pool is
    KXCRYPTOLEAD15M, a series we do not trade, and it is LIQUIDITY anyway.
  - Ceiling if a volume pool returned across all six: ~$0.005 x ~4k contracts/day
    = ~$20/day, ~$600/mo, against a fee spend of ~$650/mo. Pro-rata share will be
    less. Worth catching; not worth restructuring the strategy around.

    python3 scripts/incentive_watch.py            # our six, live + historical
    python3 scripts/incentive_watch.py --all      # every covered series

No auth: /incentive_programs is public.
"""
import argparse, collections, datetime as D, json, re, sys, urllib.request

BASE = "https://external-api.kalshi.com/trade-api/v2/incentive_programs"
# Keep in sync with LC_SERIES in scripts/kstat.py.
LC = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
PAGE = 1000


def pt(x):
    """Kalshi returns variable sub-second precision; pad to 6 digits (as _pts does)."""
    x = (x or "").replace("Z", "+00:00")
    x = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), x)
    return D.datetime.fromisoformat(x)


def pull(kind):
    """Every program of one type. status=all, because 'active' is ordered such that
    a 1000-row page can hide our series entirely — the first version of this script
    reported a clean zero for exactly that reason. Filter to live locally instead."""
    out, cursor, pages = [], "", 0
    while True:
        url = f"{BASE}?status=all&type={kind}&limit={PAGE}"
        if cursor:
            url += f"&cursor={cursor}"
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
        batch = d.get("incentive_programs") or []
        out += batch
        cursor = d.get("cursor") or ""
        pages += 1
        if not cursor or not batch or pages > 40:
            return out, (not cursor) and len(batch) == PAGE


def series_of(t):
    return re.split(r"-", t or "")[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    now = D.datetime.now(D.timezone.utc)
    act = []

    for kind in ("volume", "liquidity"):
        progs, truncated = pull(kind)
        for p in progs:
            p["_s"] = series_of(p.get("market_ticker"))
        mine = [p for p in progs if p["_s"] in LC]
        live = [p for p in mine if pt(p["start_date"]) <= now < pt(p["end_date"])]

        print(f"\n{kind.upper():10s} {len(progs):5d} programs, "
              f"{len({p['_s'] for p in progs})} series | ours: "
              f"{len(mine)} ever, {len(live)} LIVE")
        if truncated:
            print("  WARNING: hit the page cap with no cursor — list may be "
                  "INCOMPLETE; a zero here is unproven")

        for s in LC:
            r = [p for p in mine if p["_s"] == s]
            if not r:
                continue
            lv = [p for p in r if pt(p["start_date"]) <= now < pt(p["end_date"])]
            pool = sum(p.get("period_reward", 0) for p in r) / 10000
            print(f"    {s:12s} {len(r):4d} programs  ${pool:>10,.2f} total  "
                  f"{pt(min(p['start_date'] for p in r)).date()} -> "
                  f"{pt(max(p['end_date'] for p in r)).date()}"
                  f"{'   *** ' + str(len(lv)) + ' LIVE NOW ***' if lv else '   (ended)'}")
            if lv and kind == "volume":
                act.append(s)
        if a.all:
            for s, n in collections.Counter(p["_s"] for p in progs).most_common():
                print(f"      {s:26s} {n:5d}")

    print()
    if act:
        print("*** ACT: live VOLUME pool on " + ", ".join(sorted(set(act))) + ".")
        print("    Taker fills qualify, so this pays with no strategy change. Confirm the")
        print("    side restriction on the market page, then just keep trading.")
    else:
        print("No live VOLUME pool on our six — nothing to collect, nothing to change.")
        print("Liquidity pools, if any above, pay resting size only: we earn $0 from them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
