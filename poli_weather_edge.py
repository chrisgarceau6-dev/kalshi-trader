#!/usr/bin/env python3
"""Gate 1 — gross edge of Poligarch's WEATHER book, read straight from data-api.

The /positions endpoint already carries avgPrice (his entry), curPrice (current or
0/1 resolution), cashPnl, and redeemable per position. We just filter to weather
and aggregate. No settlement logic, no round-trip matching.

HONEST LIMITS (state these when reading the output):
  * SNAPSHOT ONLY. This is what he holds right now (open + resolved-but-unredeemed),
    NOT his full historical weather book. It's a slice, not the year.
  * avgPrice is HIS fill. It sizes the pie (is the edge fat?). It does NOT prove
    you could get that fill. That's gate 2 (live paper-trading).
  * Only the RESOLVED block is realized money. The OPEN block is mark-to-market and
    can still reverse.

usage:
    python poli_weather_edge.py                # defaults to Poligarch
    python poli_weather_edge.py 0x<wallet>     # any wallet
"""
import sys, re, time
import requests
import pandas as pd

DATA = "https://data-api.polymarket.com"
WALLET = (sys.argv[1] if len(sys.argv) > 1
          else "0xb40e89677d59665d5188541ad860450a6e2a7cc9").lower()

WEATHER = re.compile(
    r"temperature|°c|°f|fahrenheit|celsius|degrees|\btemp\b|"
    r"snow|rainfall|precip|inches of rain", re.I)


def fetch_positions(wallet):
    out, offset, LIMIT = [], 0, 500
    while True:
        r = requests.get(f"{DATA}/positions",
                         params={"user": wallet, "limit": LIMIT, "offset": offset},
                         timeout=30)
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:300]}")
            print("if 400/422, the param name likely differs — tell me and I'll "
                  "switch user-> proxyWallet/address")
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < LIMIT:
            break
        offset += LIMIT
        time.sleep(0.3)
    return out


def block(label, d):
    if d.empty:
        print(f"\n[{label}] none")
        return
    entry = d["avgPrice"]
    exitp = d["curPrice"]
    gross = exitp - entry
    inv = d["initialValue"] if "initialValue" in d else d["size"] * entry
    pnl = d["cashPnl"] if "cashPnl" in d else gross * d["size"]
    won = (exitp >= 0.98).mean()
    print(f"\n[{label}] n={len(d)}")
    print(f"  mean entry:        {entry.mean():.3f}")
    print(f"  mean exit/mark:    {exitp.mean():.3f}")
    print(f"  mean gross edge:   {gross.mean()*100:+.1f}c per contract")
    print(f"  win rate (mark~1): {won*100:.0f}%")
    print(f"  total invested:    ${inv.sum():,.0f}")
    print(f"  total PnL:         ${pnl.sum():,.0f}  "
          f"(ROI {100*pnl.sum()/max(inv.sum(),1):+.0f}%)")


def main():
    pos = fetch_positions(WALLET)
    if not pos:
        print("no positions returned — wallet empty, or endpoint/param drift")
        return
    df = pd.DataFrame(pos)
    print(f"pulled {len(df)} position records")
    print(f"fields: {sorted(df.columns.tolist())}")

    for c in ["size", "avgPrice", "curPrice", "initialValue",
              "currentValue", "cashPnl", "percentPnl"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if "title" not in df.columns or "avgPrice" not in df.columns \
            or "curPrice" not in df.columns:
        print("\nmissing a needed field (title/avgPrice/curPrice) — schema drift.")
        print("paste me the 'fields:' line above and I'll remap.")
        return

    df["is_weather"] = df["title"].fillna("").map(lambda t: bool(WEATHER.search(t)))
    w = df[df["is_weather"] & (df["size"].fillna(0) > 0)].copy()
    print(f"\n{len(w)} weather positions of {len(df)} total "
          f"({100*len(w)/max(len(df),1):.0f}% of current book)")
    if w.empty:
        print("no weather positions in this snapshot")
        return

    w["resolved"] = (w["curPrice"] >= 0.98) | (w["curPrice"] <= 0.02)
    if "redeemable" in w.columns:
        w["resolved"] = w["resolved"] | (w["redeemable"] == True)

    block("WEATHER — resolved (realized)", w[w["resolved"]])
    block("WEATHER — open (UNREALIZED mark)", w[~w["resolved"]])
    block("WEATHER — all", w)

    cols = [c for c in ["title", "avgPrice", "curPrice", "size", "cashPnl",
                        "redeemable"] if c in w.columns]
    show = w.sort_values("cashPnl", ascending=False) if "cashPnl" in w else w
    with pd.option_context("display.max_colwidth", 55, "display.width", 200):
        print("\ntop weather positions by PnL:")
        print(show[cols].head(15).to_string(index=False))

    w.to_csv("poli_weather_positions.csv", index=False)
    print("\nsaved -> poli_weather_positions.csv")


if __name__ == "__main__":
    main()
