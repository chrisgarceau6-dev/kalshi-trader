#!/usr/bin/env python3
"""Market-making backtest on liquid NON-weather markets.

THE MECHANISM BEING TESTED
---------------------------
A market maker posts a bid and ask around the current price, profiting
from the spread when both sides eventually fill. The risk is adverse
selection: if you get run over by a real directional move (news breaks,
price heads to resolution), your resting quotes get hit on the wrong
side and you're left holding inventory that loses value at settlement.

Weather is already disqualified -- your own model proved you're the
less-informed party there (Brier 0.186 vs market 0.118). This tests
whether the SAME mechanism (make a market, capture the spread) is
survivable on markets where you have no diagnosed informational
disadvantage: politics, sports, macro, etc.

METHOD
------
For each liquid non-weather market, pull its real historical price path
(Polymarket CLOB /prices-history, already verified working). Simulate a
naive symmetric market maker: quote (price - s/2, price + s/2) at every
interval, refreshed each step. Fill model: if the NEXT interval's price
is higher, assume the ask got lifted (sold); if lower, assume the bid
got hit (bought); flat = no fill. At the end of the price series, mark
any open inventory to the actual resolution value (1 if it won, 0 if it
lost -- read from the market's own outcome).

Net PnL = spread captured across all fills, minus inventory
mark-to-resolution. This is the real number: does spread income survive
contact with the tail risk of settlement.

usage:
    python mm_backtest.py --probe
    python mm_backtest.py --n-markets 20 --spread 0.02
"""
import argparse, json, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def toks_of(m):
    t = m.get("clobTokenIds")
    try:
        return json.loads(t) if isinstance(t, str) else t
    except Exception:
        return None


def price_history(token, start_ts, end_ts, fidelity=60):
    try:
        r = requests.get(f"{CLOB}/prices-history", params={
            "market": token, "startTs": int(start_ts), "endTs": int(end_ts),
            "fidelity": fidelity}, timeout=25)
        if r.status_code != 200:
            return None, r.status_code
        j = r.json()
        pts = j.get("history") if isinstance(j, dict) else j
        return pts, 200
    except Exception as e:
        return None, str(e)


def probe():
    print("finding one closed, non-weather, high-volume event...")
    r = requests.get(f"{GAMMA}/events", params={
        "closed": "true", "limit": 100, "order": "volume24hr",
        "ascending": "false"}, timeout=30)
    evs = [e for e in r.json()
          if "temperature" not in str(e.get("title") or "").lower()]
    if not evs:
        print("none found"); return
    e = evs[0]
    print(f"event: {e.get('title')}")
    ms = e.get("markets") or []
    if not ms:
        print("no markets on this event"); return
    m = ms[0]
    print(f"market: {m.get('question')}  outcome: {m.get('outcome')}")
    toks = toks_of(m)
    print(f"tokens: {toks}")
    if not toks:
        return
    end = int(pd.Timestamp.utcnow().timestamp())
    start = end - 30 * 86400
    pts, code = price_history(toks[0], start, end, fidelity=60)
    print(f"status: {code}  points: {len(pts) if pts else 0}")
    if pts:
        print(f"first: {pts[0]}  last: {pts[-1]}")


def resolution_value(m):
    """1.0 if this token/outcome won, 0.0 if it lost. Best-effort from
    whatever the API exposes; falls back to last known price if unclear."""
    for key in ("outcomePrices",):
        v = m.get(key)
        if v:
            try:
                arr = json.loads(v) if isinstance(v, str) else v
                p = f(arr[0]) if arr else None
                if p is not None:
                    return 1.0 if p > 0.5 else 0.0
            except Exception:
                pass
    return None


def simulate(prices, spread):
    """prices: list of floats in time order. Returns dict of pnl stats."""
    cash, position, fills = 0.0, 0.0, 0
    for i in range(len(prices) - 1):
        p, p2 = prices[i], prices[i + 1]
        bid, ask = max(p - spread / 2, 0.001), min(p + spread / 2, 0.999)
        if p2 > ask:
            cash += ask; position -= 1; fills += 1
        elif p2 < bid:
            cash -= bid; position += 1; fills += 1
    return cash, position, fills


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--n-markets", type=int, default=20)
    p.add_argument("--pages", type=int, default=4)
    p.add_argument("--spread", type=float, default=0.02,
                   help="total bid-ask width you'd quote, in dollars")
    p.add_argument("--lookback-days", type=int, default=30)
    p.add_argument("--out", default="mm_backtest.csv")
    a = p.parse_args()

    if a.probe:
        probe(); return

    print("pulling closed non-weather events...")
    events, offset = [], 0
    for _ in range(a.pages):
        r = requests.get(f"{GAMMA}/events", params={
            "closed": "true", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        events += [e for e in batch
                  if "temperature" not in str(e.get("title") or "").lower()]
        offset += 100
        time.sleep(0.15)
    events = events[:a.n_markets]
    print(f"  testing {len(events)} events")

    results = []
    for e in events:
        ms = e.get("markets") or []
        if not ms:
            continue
        m = ms[0]
        toks = toks_of(m)
        if not toks:
            continue
        outcome_val = resolution_value(m)

        end = int(pd.Timestamp.utcnow().timestamp())
        start = end - a.lookback_days * 86400
        pts, code = price_history(toks[0], start, end, fidelity=60)
        time.sleep(0.2)
        if not pts or len(pts) < 20:
            continue
        prices = [f(pt.get("p")) for pt in pts if f(pt.get("p")) is not None]
        if len(prices) < 20:
            continue

        cash, position, fills = simulate(prices, a.spread)
        final_val = outcome_val if outcome_val is not None else prices[-1]
        pnl = cash + position * final_val

        results.append({
            "event": str(e.get("title"))[:44],
            "n_points": len(prices), "fills": fills,
            "spread_pnl_gross": round(cash + position * prices[-1], 2),
            "final_position": round(position, 1),
            "outcome_value": final_val,
            "total_pnl_at_resolution": round(pnl, 2),
        })

    if not results:
        print("\nno usable markets -- run --probe to check why "
              "prices-history is coming back empty")
        return

    R = pd.DataFrame(results).sort_values("total_pnl_at_resolution")
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 200, "display.max_colwidth", 46):
        print(f"\n=== {len(R)} markets, {a.spread*100:.0f}c quoted spread ===")
        print(R.to_string(index=False))

    total = R.total_pnl_at_resolution.sum()
    n_neg = (R.total_pnl_at_resolution < 0).sum()
    print(f"\ntotal PnL across all markets: ${total:,.2f}")
    print(f"markets ending net negative: {n_neg} of {len(R)}")
    print(f"mean fills per market: {R.fills.mean():.1f}")

    print("\n--- READ THIS ---")
    print("total_pnl_at_resolution = spread captured MINUS inventory")
    print("marked to the real outcome. If most markets are mildly positive")
    print("but a few are sharply negative, that IS adverse selection showing")
    print("up exactly where it should -- the rare correlated move that runs")
    print("over your quotes right before resolution. Compare total PnL to")
    print(f"what {a.spread*100:.0f}c/fill * total fills would suggest if there")
    print("were no adverse selection at all -- the gap between those two")
    print("numbers IS the cost of making markets you don't have an edge in.")


if __name__ == "__main__":
    main()
