#!/usr/bin/env python3
"""Step 3b: re-pull with OHLC, so MAKER fills can be modelled.

WHY THIS EXISTS
---------------
pull.py stores only the CLOSE of yes_bid / yes_ask. That is enough to score a TAKER
(pay the ask, now) and useless for scoring a MAKER (rest a bid, get filled if someone
sells into it). scan.py's residual is `(won - ask) - fee` — every cell it has ever
printed is a taker entry. CLAUDE.md 2026-08-25: maker fills are FREE (998 maker
contracts at $0.00 vs 0.5493c/ct taker) and that fact is untested.

The candlestick endpoint returns full OHLC for three separate series:
    price     -> actual TRADE prints in that minute (open/high/low/close/mean)
    yes_bid   -> best bid  (open/high/low/close)
    yes_ask   -> best ask  (open/high/low/close)

`price.low` is what makes a fill model possible: a resting YES bid at B was filled in
that minute iff someone traded at or below B, i.e. `price_low <= B`. No trade prints,
no fill — which is also why the zero-volume minutes matter (weather dailies are 60-90%
of them; see maker_scan.py header).

Rows are per (market, minute, side). NO-side fields are the mirror of the YES book:
    no_bid  = 100 - yes_ask,  no_ask = 100 - yes_bid
and the same inversion maps highs to lows, which is done explicitly below rather than
left for the consumer to get wrong (the v5.16 orderbook-inversion bug).

    python3 research/search2/pull_ohlc.py --list
    python3 research/search2/pull_ohlc.py --tier1
    python3 research/search2/pull_ohlc.py --series KXBTC15M KXETH15M

Writes research/search2/data_ohlc/<SERIES>.csv.gz. Resumable: a series already on
disk is skipped unless --refresh.
"""
import argparse, csv, gzip, json, os, sys, threading, time
import concurrent.futures as cf
import datetime as D
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1]
sys.path.insert(0, str(BASE))
DATA = HERE / "data_ohlc"
UNIVERSE = HERE / "universe.json"

FIELDS = ["series", "ticker", "close_ts", "ts", "secs_left", "side",
          "bid_o", "bid_h", "bid_l", "bid_c",
          "ask_o", "ask_h", "ask_l", "ask_c",
          "px_o", "px_h", "px_l", "px_c", "px_mean",
          "volume", "open_interest", "won", "strike"]

# Flow, not headline volume, decides where a maker can live. Ranked by the fraction of
# MINUTES that contain a trade at all (measured on the step-3 archive):
#   15M series      -> 0% empty minutes
#   KXBTCD          -> 58% empty
#   weather dailies -> 60-89% empty
#   commodity dailies -> 84-90% empty
TIER1 = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M",
         "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M"]
# Kept as a CONTROL, not a candidate: if the maker edge is real it should shrink or
# vanish where there is no flow. A result that looks identical in a dead market is
# measuring the model, not the market.
TIER2 = ["KXBTCD", "KXINXU", "KXINX", "KXAAAGASD", "KXAAAGASW",
         "KXHIGHLAX", "KXHIGHNY", "KXRAIN", "KXWTI", "KXNETFLIXRANKSHOW",
         "KXAPRPOTUS", "KXTRUTHSOCIAL", "KXALBUMEQUIV", "KXGOLDD", "KXBRENTD"]

WINDOW = {"fifteen_min": (1800, 1), "hourly": (7200, 1),
          "daily": (86400, 1), "weekly": (604800, 60)}
DEFAULT_WINDOW = (7200, 1)


def _dotenv():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Retry:
    """Wraps the auth module's get(). Kalshi resets connections under a sustained
    pull -- CLAUDE.md records the same failure mode taking the trader down on
    2026-08-23. A bulk read that dies at market 400 of 600 wastes the whole series,
    so retry here rather than in kalshi_auth.py (live-path file, do not touch)."""

    def __init__(self, api, tries=5, base=1.0):
        self.api, self.tries, self.base = api, tries, base
        self.resets = 0

    def get(self, path, params=None):
        for i in range(self.tries):
            try:
                return self.api.get(path, params)
            except Exception as e:            # noqa: BLE001 - network layer, any of them
                self.resets += 1
                if i == self.tries - 1:
                    print(f"      GIVE UP {path}: {type(e).__name__}", flush=True)
                    return 0, {}
                time.sleep(self.base * (2 ** i))
        return 0, {}


def close_ts(m):
    try:
        return int(D.datetime.fromisoformat(
            m["close_time"].replace("Z", "+00:00")).timestamp())
    except (KeyError, ValueError, TypeError):
        return None


def settled_markets(api, series, max_pages=40):
    out, cur, pages = [], None, 0
    while pages < max_pages:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cur:
            p["cursor"] = cur
        code, r = api.get("/markets", p)
        if code != 200:
            break
        mk = r.get("markets", [])
        if not mk:
            break
        out += mk
        pages += 1
        cur = r.get("cursor")
        if not cur:
            break
        time.sleep(0.08)
    return out


def _ohlc(d, keys=("open", "high", "low", "close")):
    """(o,h,l,c) in cents, or None if the block is absent/unparseable."""
    if not isinstance(d, dict):
        return None
    out = []
    for k in keys:
        v = d.get(f"{k}_dollars")
        if v is None:
            return None
        try:
            out.append(float(v) * 100)
        except (TypeError, ValueError):
            return None
    return out


def market_rows(api, series, m, window):
    lookback, interval = window
    cts = close_ts(m)
    result = m.get("result")
    if cts is None or result not in ("yes", "no"):
        return []
    code, r = api.get(f"/series/{series}/markets/{m['ticker']}/candlesticks",
                      {"start_ts": cts - lookback, "end_ts": cts,
                       "period_interval": interval})
    if code != 200:
        return []
    strike = m.get("floor_strike")
    rows = []
    for c in r.get("candlesticks", []):
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        b = _ohlc(c.get("yes_bid"))
        a = _ohlc(c.get("yes_ask"))
        if b is None or a is None:
            continue
        # price/* is absent in a minute with no trades. That is a real state, not a
        # gap: it means a resting order could not have been filled. Keep the row and
        # mark it, rather than dropping it and biasing the fill rate upward.
        p = _ohlc(c.get("price"))
        pm = None
        if p is not None:
            try:
                pm = float(c["price"]["mean_dollars"]) * 100
            except (KeyError, TypeError, ValueError):
                pm = None
        vol = c.get("volume") or c.get("volume_fp") or 0
        oi = c.get("open_interest") or c.get("open_interest_fp") or 0
        f = lambda x: "" if x is None else f"{x:.4f}"

        def emit(side, bo, bh, bl, bc, ao, ah, al, ac, po, ph, pl, pc, pmean):
            rows.append([series, m["ticker"], cts, ts, cts - ts, side,
                         f(bo), f(bh), f(bl), f(bc), f(ao), f(ah), f(al), f(ac),
                         f(po), f(ph), f(pl), f(pc), f(pmean),
                         vol, oi, result == side, strike])

        emit("yes", b[0], b[1], b[2], b[3], a[0], a[1], a[2], a[3],
             p[0] if p else None, p[1] if p else None,
             p[2] if p else None, p[3] if p else None, pm)
        # NO book is the mirror: no_bid = 100 - yes_ask. Mirroring flips high and low.
        inv = lambda x: None if x is None else 100 - x
        emit("no", inv(a[0]), inv(a[2]), inv(a[1]), inv(a[3]),
             inv(b[0]), inv(b[2]), inv(b[1]), inv(b[3]),
             inv(p[0]) if p else None, inv(p[2]) if p else None,
             inv(p[1]) if p else None, inv(p[3]) if p else None, inv(pm))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="+")
    ap.add_argument("--tier1", action="store_true")
    ap.add_argument("--tier2", action="store_true")
    ap.add_argument("--max-markets", type=int, default=8000,
                    help="~6,400 covers the full ~67-day retention for a 15M series")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    uni = {u["ticker"]: u for u in json.loads(UNIVERSE.read_text())}
    targets = a.series or ((TIER1 if a.tier1 else []) + (TIER2 if a.tier2 else []))
    if not targets:
        targets = TIER1
    if a.list:
        for t in targets:
            u = uni.get(t, {})
            print(f"{t:<22}{u.get('freq',''):<14}{u.get('volume',0):>13,}")
        return 0

    DATA.mkdir(parents=True, exist_ok=True)
    _dotenv()
    import kalshi_auth as _K
    K = Retry(_K)

    t0 = time.time()
    for n, s in enumerate(targets, 1):
        out = DATA / f"{s}.csv.gz"
        if out.exists() and not a.refresh:
            print(f"[{n}/{len(targets)}] {s}: cached, skipping", flush=True)
            continue
        window = WINDOW.get((uni.get(s) or {}).get("freq"), DEFAULT_WINDOW)
        mk = [m for m in settled_markets(K, s) if m.get("result") in ("yes", "no")]
        mk = mk[:a.max_markets]
        print(f"[{n}/{len(targets)}] {s}: {len(mk)} settled markets, window={window}",
              flush=True)
        # Threaded. A 15M series runs 96 markets/day, so the ~67-day retention window
        # is ~6,400 markets = 6,400 candlestick calls, and the run is entirely network
        # latency. Serial that is ~30 min/series; 6 workers brings it under 6. Kept
        # modest deliberately -- this shares an API key with the LIVE trader, and
        # rate-limiting the bot to win a research pull would be a catastrophic trade.
        rows = []
        done = [0]
        lock = threading.Lock()

        def work(m):
            r = market_rows(K, s, m, window)
            with lock:
                rows.extend(r)
                done[0] += 1
                if done[0] % 500 == 0:
                    print(f"      {done[0]}/{len(mk)}  {len(rows):,} rows  "
                          f"{time.time()-t0:.0f}s", flush=True)
            return None

        with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(work, mk))
        rows.sort(key=lambda r: (r[1], r[5], r[3]))
        tmp = out.with_suffix(".tmp")
        with gzip.open(tmp, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            w.writerows(rows)
        tmp.rename(out)
        print(f"      -> {out.name}  {len(rows):,} rows  {time.time()-t0:.0f}s",
              flush=True)
    print(f"done in {time.time()-t0:.0f}s, {K.resets} network retries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
