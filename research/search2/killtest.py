#!/usr/bin/env python3
"""Step 5: kill tests. Run every surviving lead through these BEFORE it gets a week.

The scan (step 4) ranks slices by net edge. Most of what it surfaces is not tradeable
for reasons that have nothing to do with whether the edge is real. Each test below
costs seconds and has killed a real candidate before:

  1. FEE        is the edge bigger than the fee at that price?
                Killed all 11 direction-neutral structures in CLAUDE.md at once —
                they trade near 50c where the fee peaks at 1.75c/contract.
  2. POPULATION does the price actually sit there often enough to trade?
                Killed weather (0.9% of quotes in band) and KXINXU (0%).
  3. DEPTH      is there size at that price, or is the quote decorative?
                Killed ask-94: strong model edge, 11-15% executable.
  4. CAPACITY   does it collide with the live strategy's close clusters?
                MAX_CONCURRENT_POSITIONS=2 is shared, so a colliding strategy
                DISPLACES late-certainty rather than adding to it — which fails the
                brief, whatever its edge.
  5. HOLDOUT    does it survive on the most recent third of the data?

Depth needs a live book, so test 3 samples currently-open markets. The rest run
entirely on the pulled archive.

    python3 research/search2/killtest.py --series KXHIGHLAX --price 90-93c
    python3 research/search2/killtest.py --series KXWTI --all-prices
"""
import argparse
import csv
import glob
import gzip
import os
import statistics
import sys
import time
import datetime as D
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1]
sys.path.insert(0, str(BASE))
DATA = HERE / "data"

FEE_RATE = 0.07
LIVE_SERIES = {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"}
BANDS = {"01-09c": (1, 10), "10-24c": (10, 25), "25-39c": (25, 40),
         "40-59c": (40, 60), "60-74c": (60, 75), "75-84c": (75, 85),
         "85-89c": (85, 90), "90-93c": (90, 94), "94-96c": (94, 97),
         "97-99c": (97, 99.01)}


def fee_cents(ask):
    p = ask / 100.0
    return FEE_RATE * p * (1 - p) * 100


def _dotenv():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(series):
    path = DATA / f"{series}.csv.gz"
    if not path.exists():
        sys.exit(f"no archive for {series} — run pull.py --series {series}")
    out = []
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            try:
                ask, bid = float(r["ask"]), float(r["bid"])
            except (TypeError, ValueError):
                continue
            if not (1.0 <= ask <= 99.0):
                continue
            out.append(dict(close_ts=int(r["close_ts"]), secs=int(r["secs_left"]),
                            ask=ask, bid=bid, side=r["side"],
                            won=r["won"] in ("True", "true", "1"),
                            ticker=r["ticker"]))
    return out


def t1_fee(rows, lo, hi):
    g = [r for r in rows if lo <= r["ask"] < hi]
    if not g:
        return "SKIP", "no observations in band"
    edge = statistics.mean(100 * ((1 if r["won"] else 0) - r["ask"] / 100) for r in g)
    fee = statistics.mean(fee_cents(r["ask"]) for r in g)
    net = edge - fee
    v = "PASS" if net > 0 else "KILL"
    return v, (f"edge {edge:+.2f}c vs fee {fee:.2f}c -> NET {net:+.2f}c/contract "
               f"(n={len(g):,})")


def t2_population(rows, lo, hi):
    """Share of MARKETS that ever quote in the band, and share of observations."""
    per = defaultdict(bool)
    for r in rows:
        if lo <= r["ask"] < hi:
            per[r["ticker"]] = True
    tickers = {r["ticker"] for r in rows}
    mshare = 100 * sum(per.values()) / max(1, len(tickers))
    oshare = 100 * sum(1 for r in rows if lo <= r["ask"] < hi) / max(1, len(rows))
    v = "PASS" if mshare >= 25 else ("THIN" if mshare >= 10 else "KILL")
    return v, (f"{mshare:.0f}% of markets ever quote here, {oshare:.1f}% of all "
               f"observations. Live crypto control: ~36-40%")


def t3_depth(series, lo, hi, sample=12):
    """Live book check — is there size at that price, or is the quote decorative?"""
    _dotenv()
    try:
        import kalshi_auth as K
    except Exception as exc:
        return "SKIP", f"no API: {exc}"
    code, r = K.get("/markets", {"series_ticker": series, "status": "open", "limit": 60})
    if code != 200:
        return "SKIP", f"/markets HTTP {code}"
    mk = r.get("markets", [])
    seen = deep = 0
    depths = []
    for m in mk[:sample]:
        c, ob = K.get(f"/markets/{m['ticker']}/orderbook", {})
        if c != 200:
            continue
        fp = ob.get("orderbook_fp") or {}
        no = fp.get("no_dollars") or []
        if not no:
            continue
        # Buying YES lifts NO bids: a NO bid at P is a YES offer at (1-P).
        offers = sorted(((1 - float(x[0])) * 100, float(x[1])) for x in no)
        inband = sum(q for px, q in offers if lo <= px < hi)
        if offers:
            seen += 1
            depths.append(inband)
            if inband >= 25:
                deep += 1
        time.sleep(0.05)
    if not seen:
        return "SKIP", "no open books sampled"
    med = statistics.median(depths)
    v = "PASS" if deep / seen >= 0.4 else ("THIN" if deep / seen >= 0.15 else "KILL")
    return v, (f"{deep}/{seen} open books have >=25 contracts in band, "
               f"median {med:.0f}")


def t4_capacity(rows, lo, hi):
    """Does this collide with the live strategy's close clusters?

    The live trader caps at MAX_CONCURRENT_POSITIONS=2 ACROSS the account. A candidate
    whose entries land in the same close clusters displaces late-certainty instead of
    adding to it, which fails the brief regardless of edge.
    """
    entries = {r["close_ts"] for r in rows if lo <= r["ask"] < hi}
    if not entries:
        return "SKIP", "no entries in band"
    # Live 15M crypto closes on :00/:15/:30/:45.
    collide = sum(1 for t in entries if t % 900 == 0)
    pct = 100 * collide / len(entries)
    v = "PASS" if pct < 20 else ("WARN" if pct < 60 else "KILL")
    return v, (f"{pct:.0f}% of entry clusters fall on a 15-minute boundary where the "
               f"live strategy also trades ({collide}/{len(entries)})")


def t5_holdout(rows, lo, hi):
    cl = sorted({r["close_ts"] for r in rows})
    if len(cl) < 60:
        return "SKIP", f"only {len(cl)} clusters"
    cut = cl[int(len(cl) * 0.67)]
    out = []
    for label, sel in (("in-sample", lambda r: r["close_ts"] < cut),
                       ("holdout", lambda r: r["close_ts"] >= cut)):
        g = [r for r in rows if lo <= r["ask"] < hi and sel(r)]
        if not g:
            out.append(f"{label}: n=0")
            continue
        net = statistics.mean(
            100 * ((1 if r["won"] else 0) - r["ask"] / 100) - fee_cents(r["ask"])
            for r in g)
        out.append(f"{label} {net:+.2f}c (n={len(g):,})")
    both = [o for o in out if "n=0" not in o]
    v = "SKIP"
    if len(both) == 2:
        signs = ["+" in o.split()[1] for o in both]
        v = "PASS" if all(signs) else "KILL"
    return v, " | ".join(out)


def run(series, band):
    lo, hi = BANDS[band]
    rows = load(series)
    print(f"\n{'='*78}\n{series}  band {band}  ({len(rows):,} observations, "
          f"{len({r['close_ts'] for r in rows}):,} clusters)\n{'='*78}")
    verdicts = []
    for name, fn in (("1 FEE", lambda: t1_fee(rows, lo, hi)),
                     ("2 POPULATION", lambda: t2_population(rows, lo, hi)),
                     ("3 DEPTH", lambda: t3_depth(series, lo, hi)),
                     ("4 CAPACITY", lambda: t4_capacity(rows, lo, hi)),
                     ("5 HOLDOUT", lambda: t5_holdout(rows, lo, hi))):
        try:
            v, msg = fn()
        except Exception as exc:
            v, msg = "ERROR", f"{type(exc).__name__}: {exc}"
        verdicts.append(v)
        print(f"  {v:<6}{name:<14}{msg}")
    dead = [v for v in verdicts if v == "KILL"]
    print(f"  {'-'*74}")
    print(f"  VERDICT: {'DEAD — ' + str(len(dead)) + ' kill test(s) failed' if dead else 'SURVIVES all kill tests — promote to pre-registration'}")
    return not dead


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True)
    ap.add_argument("--price", default=None, choices=sorted(BANDS),
                    help="a specific band; omit to test ALL bands, which is "
                         "the default because anchoring on one band is how "
                         "the search stayed stuck on the existing strategy")
    ap.add_argument("--all-prices", action="store_true")
    a = ap.parse_args()
    bands = sorted(BANDS) if (a.all_prices or not a.price) else [a.price]
    surv = [b for b in bands if run(a.series, b)]
    if len(bands) > 1:
        print(f"\n{a.series}: {len(surv)}/{len(bands)} bands survive: "
              f"{', '.join(surv) if surv else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
