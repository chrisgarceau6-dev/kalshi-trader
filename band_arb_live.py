#!/usr/bin/env python3
"""Live band-arbitrage scanner for Polymarket multi-outcome events.

THE TEST
--------
A city-day temperature event is mutually exclusive and collectively
exhaustive: exactly one band resolves YES. So the ASKS across the full
band set must sum to >= $1.00 in an efficient market. If they sum to
less, you buy every band and are guaranteed to hold exactly one winner
that pays $1.00 — riskless, no forecasting required.

WHY THE PREVIOUS ATTEMPT FAILED
-------------------------------
band_arb_check.py inferred the band set from one wallet's holdings and
used sum(curPrice)==1 as a completeness proof. That condition is true
whenever the wallet held the WINNING band, so it silently selected only
the city-days that wallet won. Outcome selection. This version never
looks at any wallet — it takes the whole band list from event metadata,
so completeness is structural rather than inferred.

WHY ASKS, NOT MID
-----------------
You buy at the ask. A mid-price sum below 1.00 is not tradeable; only
the sum of asks is. The script uses bestAsk when the API supplies it and
reports how many legs were missing a quote, because a set with a missing
leg is not a set.

usage:
    python band_arb_live.py --probe          # dump raw API shapes first
    python band_arb_live.py                  # scan temperature events
    python band_arb_live.py --pattern "highest temperature" --fee 0.02
    python band_arb_live.py --pattern ""     # every open multi-outcome event
"""
import argparse, json, re, sys, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def jparse(v, default=None):
    """gamma returns several fields as JSON-encoded strings."""
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def fetch_events(limit_pages, page=100, closed=False):
    out, offset = [], 0
    for _ in range(limit_pages):
        try:
            r = requests.get(f"{GAMMA}/events",
                             params={"closed": str(closed).lower(),
                                     "limit": page, "offset": offset},
                             timeout=30)
        except Exception as e:
            print(f"  net error: {type(e).__name__}"); break
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} from /events: {r.text[:200]}")
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
        time.sleep(0.2)
    return out


def clob_ask(token_id):
    """Fallback when gamma has no bestAsk. Returns lowest ask or None."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=15)
        if r.status_code != 200:
            return None
        asks = r.json().get("asks") or []
        if not asks:
            return None
        return min(float(a["price"]) for a in asks)
    except Exception:
        return None


def probe():
    print("=== PROBE: one event, raw shape ===")
    ev = fetch_events(1, page=3)
    if not ev:
        print("no events returned — endpoint or params wrong")
        return
    e = ev[0]
    print("event keys:", sorted(e.keys()))
    print("title:", e.get("title"))
    ms = e.get("markets") or []
    print(f"markets under this event: {len(ms)}")
    if ms:
        m = ms[0]
        print("market keys:", sorted(m.keys()))
        for k in ("question", "outcomes", "outcomePrices", "bestAsk", "bestBid",
                  "clobTokenIds", "closed"):
            print(f"  {k}: {str(m.get(k))[:120]}")
    print("\nif bestAsk is absent above, the script falls back to CLOB /book")
    tid = None
    if ms:
        tids = jparse(ms[0].get("clobTokenIds"), [])
        tid = tids[0] if tids else None
    if tid:
        print(f"\nCLOB /book probe for token {tid[:16]}...: ask={clob_ask(tid)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pattern", default="highest temperature",
                   help="regex to match event titles; empty string = all")
    p.add_argument("--pages", type=int, default=8)
    p.add_argument("--fee", type=float, default=0.0,
                   help="dollar cushion per set for fees/slippage")
    p.add_argument("--min-bands", type=int, default=3)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--out", default="band_arb_live.csv")
    a = p.parse_args()

    if a.probe:
        probe(); return

    print(f"pulling open events from gamma (up to {a.pages} pages)...")
    events = fetch_events(a.pages)
    print(f"  {len(events)} open events")
    if not events:
        print("nothing returned — run with --probe to inspect the endpoint")
        return

    rx = re.compile(a.pattern, re.I) if a.pattern else None
    rows, skipped_noquote = [], 0

    for e in events:
        title = str(e.get("title") or "")
        if rx and not rx.search(title):
            continue
        ms = [m for m in (e.get("markets") or []) if not m.get("closed")]
        if len(ms) < a.min_bands:
            continue

        asks, missing = [], 0
        for m in ms:
            ask = m.get("bestAsk")
            if ask is None:
                tids = jparse(m.get("clobTokenIds"), [])
                ask = clob_ask(tids[0]) if tids else None
                time.sleep(0.05)
            if ask is None:
                missing += 1
            else:
                asks.append(float(ask))
        if missing:
            skipped_noquote += 1
            continue

        s = sum(asks)
        rows.append({"event": title[:70], "bands": len(asks),
                     "ask_sum": round(s, 4),
                     "edge": round(1.0 - s - a.fee, 4)})

    if skipped_noquote:
        print(f"  {skipped_noquote} events skipped — at least one leg had no quote "
              f"(an incomplete set is not a set)")

    if not rows:
        print("\nno complete multi-band sets matched. widen with --pattern '' "
              "or raise --pages")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)

    with pd.option_context("display.width", 200, "display.max_colwidth", 72):
        print(f"\n=== {len(R)} complete band sets, sum of ASKS vs $1.00 ===")
        print(R.head(30).to_string(index=False))

    n_arb = (R.edge > 0).sum()
    print(f"\nsets buyable below $1.00 after {a.fee*100:.0f}c cushion: "
          f"{n_arb} of {len(R)} ({n_arb/len(R):.0%})")
    print(f"mean ask_sum:   {R.ask_sum.mean():.4f}")
    print(f"median ask_sum: {R.ask_sum.median():.4f}")
    print(f"min ask_sum:    {R.ask_sum.min():.4f}  ({R.iloc[0].event})")

    print("\nHOW TO READ THIS:")
    print("  ask_sum >= 1.00 everywhere = no arb, the market is doing its job.")
    print("  Any ask_sum well below 1.00 is either a real arb or a stale/thin")
    print("  quote. Check depth before believing it — best ask with 5 contracts")
    print("  behind it is not a tradeable edge at any size.")
    print(f"\nsaved -> {a.out}")


if __name__ == "__main__":
    main()
