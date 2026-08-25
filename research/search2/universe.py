#!/usr/bin/env python3
"""Step 2 of the strategy-2 search: map what is actually searchable on Kalshi.

CONSTRAINT SHEET (Chris, 2026-08-25):
  capital   shares the account, weighted by edge quality. HARD CEILING $2,000.
  venue     Kalshi only.
  holding   unconstrained — daily/weekly are in scope, not just 15M/hourly.
  automation preferred, negotiable.

WHY THIS EXISTS
---------------
The search for a second strategy has been idea-first: think of something, spend a
week testing it. ~30 ideas, one marginal survivor. The binding constraint on ever
PROVING anything is not cleverness, it is independent settled observations inside
Kalshi's ~67-day retention window. So enumerate that first and let it decide where
the search is even possible.

The key quantity is not settlement frequency. It is observations per day:

    obs/day  =  events per day  x  strikes per event

A daily series with 20 strikes yields ~1,340 observations in the retention window —
more than an hourly series with one strike. Multi-strike ladders are how a slow
market becomes statistically searchable, which is why holding period being
unconstrained matters so much.

FIELD TRAP (CLAUDE.md): the markets endpoint uses volume_fp / open_interest_fp.
Querying volume / open_interest returns 0 for every series INCLUDING ones the bot is
actively filling, which reads as "market is dead".

    python3 research/search2/universe.py            # writes universe.json
    python3 research/search2/universe.py --top 40   # print the ranked head
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
OUT = Path(__file__).resolve().parent / "universe.json"

# Frequencies that can plausibly accumulate a sample inside ~67 days of retention.
# 'custom' and 'one_off' are 11,024 of 13,448 series and can never accumulate one.
SEARCHABLE_FREQ = {"fifteen_min", "hourly", "daily", "weekly"}
RETENTION_DAYS = 67


def _dotenv():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def survey(series_ticker, api):
    """Liquidity and observation density for one series, from its settled markets."""
    code, r = api.get("/markets", {"series_ticker": series_ticker,
                                   "status": "settled", "limit": 200})
    if code != 200:
        return None
    mk = r.get("markets", [])
    if not mk:
        return None
    by_close = defaultdict(int)
    vol = oi = 0.0
    resolved = 0
    for m in mk:
        by_close[m.get("close_time", "")] += 1
        vol += float(m.get("volume_fp", 0) or 0)
        oi += float(m.get("open_interest_fp", 0) or 0)
        if m.get("result") in ("yes", "no"):
            resolved += 1
    # Density from the TIME SPAN the returned markets cover, not from distinct
    # calendar days. Counting days makes a dense ladder whose 200 rows all land in one
    # afternoon read as "200/day" — the API cap reported as a measurement. Full
    # pagination is not an alternative: KXBTCD is ~1,500 pages inside retention.
    times = sorted(c for c in by_close if c)
    hours = 0.0
    if len(times) > 1:
        try:
            import datetime as _D
            lo = _D.datetime.fromisoformat(times[0].replace("Z", "+00:00"))
            hi = _D.datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
            hours = (hi - lo).total_seconds() / 3600.0
        except ValueError:
            hours = 0.0
    truncated = len(mk) >= 200
    if hours > 0:
        obs_day = len(mk) / (hours / 24.0)
        exact = True
    else:
        obs_day = float(len(mk))          # single close time; cannot resolve density
        exact = False
    strikes = len(mk) / max(1, len(by_close))
    return dict(
        ticker=series_ticker,
        n_markets=len(mk), resolved=resolved,
        events=len(by_close), strikes_per_event=round(strikes, 1),
        span_hours=round(hours, 2),
        obs_per_day=round(obs_day, 1),
        # Capped at what retention can actually hold: density measured over a 2-hour
        # window does not mean that rate held for 67 days.
        obs_in_retention=int(round(min(obs_day * RETENTION_DAYS, 200 * RETENTION_DAYS))),
        density_exact=exact,
        volume=int(vol), open_interest=int(oi),
        truncated=truncated,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-obs", type=int, default=300,
                    help="minimum observations in the retention window to be searchable")
    ap.add_argument("--refresh", action="store_true", help="re-pull even if cached")
    a = ap.parse_args()

    if OUT.exists() and not a.refresh:
        rows = json.loads(OUT.read_text())
        print(f"loaded {len(rows)} surveyed series from {OUT.name} (--refresh to re-pull)")
    else:
        _dotenv()
        import kalshi_auth as K
        code, r = K.get("/series", {"limit": 200})
        if code != 200:
            sys.exit(f"/series HTTP {code}")
        allser = r.get("series", [])
        cand = [s for s in allser if s.get("frequency") in SEARCHABLE_FREQ]
        print(f"{len(allser):,} series total | {len(cand)} in a searchable frequency "
              f"({', '.join(sorted(SEARCHABLE_FREQ))})")
        rows = []
        for i, s in enumerate(cand, 1):
            got = survey(s["ticker"], K)
            if got:
                got.update(freq=s.get("frequency"), category=s.get("category", ""),
                           title=(s.get("title") or "")[:70],
                           fee_type=s.get("fee_type"), fee_mult=s.get("fee_multiplier"))
                rows.append(got)
            if i % 50 == 0:
                print(f"  surveyed {i}/{len(cand)}...")
            time.sleep(0.04)
        OUT.write_text(json.dumps(rows, indent=1))
        print(f"wrote {OUT}")

    # Anything with a non-standard fee profile changes the net-edge arithmetic and
    # must not be averaged in silently.
    odd = [r for r in rows if (r.get("fee_type"), r.get("fee_mult")) != ("quadratic", 1)]
    print(f"\nfee profile: {len(rows) - len(odd)} standard (quadratic x1), {len(odd)} OTHER")
    for r in odd[:10]:
        print(f"  !! {r['ticker']:<20}{r.get('fee_type')} x{r.get('fee_mult')}")

    live = {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"}
    ok = [r for r in rows if r["obs_in_retention"] >= a.min_obs and r["volume"] > 0
          and r["ticker"] not in live]
    ok.sort(key=lambda r: -r["obs_in_retention"])
    print(f"\n{len(ok)} series with >= {a.min_obs} observations in retention and real "
          f"volume, excluding the 6 already traded\n")
    print(f"  {'series':<20}{'freq':<12}{'obs/day':>9}{'in 67d':>9}"
          f"{'strikes':>9}{'volume':>13}  category")
    print("  " + "-" * 96)
    for r in ok[:a.top]:
        t = "*" if r["truncated"] else " "
        print(f"  {r['ticker']:<20}{r['freq']:<12}{r['obs_per_day']:>9.1f}"
              f"{r['obs_in_retention']:>8,}{t}{r['strikes_per_event']:>9.1f}"
              f"{r['volume']:>13,}  {r['category'][:24]}")
    print("\n  * 200-row API cap hit — obs/day is a LOWER bound for this series")
    print("  obs_in_retention is the ceiling on what can ever be PROVEN here, not a "
          "measure of edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
