#!/usr/bin/env python3
"""Does the live [SHADOW:MOM3] value equal what the research code computes?

Two separate checks:
  A. IMPLEMENTATION — recompute m3 at the same instant the live code used
     (its wall-clock minute boundary). Must match the logged value.
  B. COMPARABILITY — the research/backtest anchors m3 at the KALSHI CANDLE
     boundary (close_ts - secs_left, always a multiple of 60), while live anchors
     at the poll instant's minute. Those differ by up to 60s. Measure how much m3
     moves under that shift, because it decides whether harvested live data can be
     compared to the -$1.56/tr threshold measured on the archive.

Usage: python3 parity_audit.py /tmp/mom3_direct.txt
"""
import json, math, re, sys, urllib.request
from datetime import datetime, timezone

PAIR = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
        "KXDOGE15M": "DOGE-USD", "KXXRP15M": "XRP-USD", "KXBNB15M": "BNB-USD"}
MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
LINE = re.compile(
    r"(?P<ts>\d{4}-\d\d-\d\dT[\d:.]+Z).*\[SHADOW:MOM3\]\s+(?P<ticker>\S+)\s+"
    r"(?P<side>YES|NO)\s+(?P<ask>[\d.]+)c\s+(?P<secs>\d+)s\s+"
    r"m3=(?P<m3>[-+][\d.]+)\s+sigma=(?P<sigma>[\d.]+)bp")

_cache = {}


def candles(pair, start, end):
    key = (pair, start, end)
    if key in _cache:
        return _cache[key]
    url = (f"https://api.exchange.coinbase.com/products/{pair}/candles"
           f"?granularity=60&start={start}&end={end}")
    req = urllib.request.Request(url, headers={"User-Agent": "parity-audit/1.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read())
    # completed buckets only — must mirror _spot_momentum, or the audit compares
    # a price one minute later than the trader used and reports a phantom mismatch
    out = {int(r[0]): float(r[4]) for r in d if int(r[0]) + 60 <= end}
    _cache[key] = out
    return out


def m3_at(series, anchor, sign):
    """Research formula, anchored so the newest bucket close is at `anchor`."""
    pair = PAIR[series]
    px = candles(pair, anchor - 64 * 60, anchor)
    mins = sorted(px)
    if not mins:
        return None, None
    rets = [math.log(px[b] / px[a]) for a, b in zip(mins, mins[1:])
            if b - a == 60 and px[a] > 0 and px[b] > 0]
    if len(rets) < 20:
        return None, None
    mu = sum(rets) / len(rets)
    sig = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    back = mins[-1] - 180
    if sig <= 0 or back not in px:
        return None, None
    mom = math.log(px[mins[-1]] / px[back]) / (sig * math.sqrt(3))
    return -sign * mom, sig


def close_ts_of(ticker):
    m = re.match(r"KX\w+?15M-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", ticker)
    yy, mon, dd, hh, mi = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    # ticker time is ET; -04:00 in August
    dt = datetime(2000 + int(yy), MON[mon], int(dd), int(hh), int(mi),
                  tzinfo=timezone(-__import__("datetime").timedelta(hours=4)))
    return int(dt.timestamp())


rows = []
for ln in open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/mom3_direct.txt"):
    m = LINE.search(ln)
    if m:
        rows.append(m.groupdict())
print(f"{len(rows)} live MOM3 observations\n")

print("A. IMPLEMENTATION CHECK — recompute at the live anchor")
print(f"  {'ticker':<26} {'side':>4} {'logged':>8} {'recomputed':>11} {'diff':>7} "
      f"{'sig log':>8} {'sig calc':>9}")
bad = 0
for r in rows:
    series = r["ticker"].split("-")[0]
    log_ts = int(datetime.fromisoformat(re.sub(r"\.\d+Z$", "+00:00", r["ts"])).timestamp())
    anchor = log_ts // 60 * 60          # exactly what the live code used
    sign = 1.0 if r["side"] == "YES" else -1.0
    got, sig = m3_at(series, anchor, sign)
    if got is None:
        print(f"  {r['ticker']:<26} {r['side']:>4}  no data")
        continue
    d = got - float(r["m3"])
    if abs(d) > 0.05:
        bad += 1
    print(f"  {r['ticker']:<26} {r['side']:>4} {float(r['m3']):>+8.2f} {got:>+11.2f} "
          f"{d:>+7.2f} {float(r['sigma']):>8.2f} {sig*1e4:>9.2f}")
print(f"\n  mismatches beyond +/-0.05: {bad}/{len(rows)}")

print("\nB. COMPARABILITY — how much does m3 move if anchored 60s earlier/later?")
print("  (research anchors at the Kalshi candle boundary; live at the poll minute)")
print(f"  {'ticker':<26} {'-60s':>8} {'live':>8} {'+60s':>8} {'spread':>8}")
spreads = []
for r in rows:
    series = r["ticker"].split("-")[0]
    log_ts = int(datetime.fromisoformat(re.sub(r"\.\d+Z$", "+00:00", r["ts"])).timestamp())
    anchor = log_ts // 60 * 60
    sign = 1.0 if r["side"] == "YES" else -1.0
    vals = []
    for off in (-60, 0, 60):
        v, _ = m3_at(series, anchor + off, sign)
        vals.append(v)
    if any(v is None for v in vals):
        continue
    sp = max(vals) - min(vals)
    spreads.append(sp)
    print(f"  {r['ticker']:<26} {vals[0]:>+8.2f} {vals[1]:>+8.2f} {vals[2]:>+8.2f} "
          f"{sp:>8.2f}")
if spreads:
    spreads.sort()
    print(f"\n  m3 spread across a +/-60s anchor shift: median {spreads[len(spreads)//2]:.2f}, "
          f"max {spreads[-1]:.2f}")
    print(f"  the live threshold under study is m3 > +0.50, so a spread of this size")
    print(f"  {'IS' if spreads[len(spreads)//2] > 0.25 else 'is NOT'} large relative to the decision boundary")
