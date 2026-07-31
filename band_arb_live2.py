#!/usr/bin/env python3
"""Live band-arbitrage scanner v2 — negRisk edition.

WHAT CHANGED FROM v1
--------------------
v1 matched event titles with a regex and found nothing across 800 events.
The probe revealed the right handle: Polymarket tags mutually-exclusive
multi-outcome events with `negRisk`. That flag IS the structure the arb
needs, so we filter on it instead of guessing at titles.

v1 also would have scored closed markets. The probe showed a closed
market carrying bestAsk 0.002 with bestBid None — junk quotes that would
manufacture a fake arb. v2 requires acceptingOrders and not closed.

THE TEST
--------
For a negRisk event, exactly one outcome resolves YES. So the asks across
the full set must sum to >= $1.00. Below that, buying every leg is
riskless: you are guaranteed to hold the one winner that pays $1.00.

Every filter stage is counted and printed, so a zero result tells you
WHERE things dropped out instead of just saying "nothing matched".

usage:
    python band_arb_live2.py
    python band_arb_live2.py --pages 30 --fee 0.02
    python band_arb_live2.py --pattern "temperature"   # optional extra filter
    python band_arb_live2.py --show-titles             # dump what IS out there
"""
import argparse, json, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"


def jparse(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def fetch_events(pages, page=100):
    out, offset = [], 0
    for i in range(pages):
        try:
            r = requests.get(f"{GAMMA}/events",
                             params={"closed": "false", "limit": page,
                                     "offset": offset, "order": "volume24hr",
                                     "ascending": "false"},
                             timeout=30)
        except Exception as e:
            print(f"  net error on page {i}: {type(e).__name__}"); break
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {r.text[:200]}"); break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
        time.sleep(0.2)
    return out


def live_markets(e):
    """Markets that are actually tradeable right now."""
    out = []
    for m in (e.get("markets") or []):
        if m.get("closed"):
            continue
        if m.get("acceptingOrders") is False:
            continue
        out.append(m)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=20)
    p.add_argument("--fee", type=float, default=0.0)
    p.add_argument("--min-bands", type=int, default=3)
    p.add_argument("--pattern", default="", help="optional extra title regex")
    p.add_argument("--show-titles", action="store_true")
    p.add_argument("--out", default="band_arb_live.csv")
    a = p.parse_args()

    print(f"pulling open events (up to {a.pages} pages, by 24h volume)...")
    events = fetch_events(a.pages)
    print(f"  stage 1 — open events pulled:            {len(events)}")

    negrisk = [e for e in events
               if e.get("negRisk") or e.get("enableNegRisk")
               or e.get("negRiskAugmented")]
    print(f"  stage 2 — flagged negRisk (MECE sets):   {len(negrisk)}")

    if a.show_titles:
        print("\n  sample negRisk event titles:")
        for e in negrisk[:30]:
            print(f"    {len(e.get('markets') or []):>3} mkts  {str(e.get('title'))[:70]}")
        if not negrisk:
            print("    (none) — sample of ALL event titles instead:")
            for e in events[:30]:
                print(f"    {len(e.get('markets') or []):>3} mkts  {str(e.get('title'))[:70]}")

    rx = re.compile(a.pattern, re.I) if a.pattern else None
    if rx:
        negrisk = [e for e in negrisk if rx.search(str(e.get("title") or ""))]
        print(f"  stage 3 — matching --pattern:            {len(negrisk)}")

    rows = []
    n_thin, n_noquote = 0, 0
    for e in negrisk:
        ms = live_markets(e)
        if len(ms) < a.min_bands:
            n_thin += 1
            continue
        asks, missing = [], 0
        for m in ms:
            ask = m.get("bestAsk")
            if ask in (None, "", 0):
                missing += 1
            else:
                asks.append(float(ask))
        if missing:
            n_noquote += 1
            continue
        s = sum(asks)
        rows.append({
            "event": str(e.get("title"))[:66],
            "bands": len(asks),
            "ask_sum": round(s, 4),
            "edge": round(1.0 - s - a.fee, 4),
            "vol24h": round(float(e.get("volume24hr") or 0)),
            "liq": round(float(e.get("liquidity") or 0)),
        })

    print(f"  stage 4 — dropped, <{a.min_bands} live markets:      {n_thin}")
    print(f"  stage 5 — dropped, a leg had no ask:     {n_noquote}")
    print(f"  stage 6 — scored:                        {len(rows)}")

    if not rows:
        print("\nnothing scored. try --pages 40, or --show-titles to see what "
              "the negRisk universe actually looks like")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)

    with pd.option_context("display.width", 220, "display.max_colwidth", 68):
        print(f"\n=== {len(R)} complete negRisk sets — sum of ASKS vs $1.00 ===")
        print(R.head(25).to_string(index=False))

    n_arb = int((R.edge > 0).sum())
    print(f"\nsets buyable under $1.00 after {a.fee*100:.0f}c cushion: "
          f"{n_arb} of {len(R)} ({n_arb/len(R):.0%})")
    print(f"mean ask_sum:   {R.ask_sum.mean():.4f}")
    print(f"median ask_sum: {R.ask_sum.median():.4f}")
    print(f"min ask_sum:    {R.ask_sum.min():.4f}")

    excess = (R.ask_sum - 1.0).median()
    print(f"\nmedian excess over $1.00: {excess*100:+.2f}c per set")
    print("  that excess is roughly what the makers collect for quoting these")
    print("  sets — i.e. it prices door #2, not door #1")

    print("\nBEFORE BELIEVING ANY POSITIVE EDGE:")
    print("  bestAsk is top-of-book only. Check depth and vol24h/liq columns —")
    print("  a 3c edge behind 5 contracts is not tradeable at size, and thin")
    print("  or stale quotes are exactly where phantom arbs appear.")
    print(f"\nsaved -> {a.out}")


if __name__ == "__main__":
    main()
