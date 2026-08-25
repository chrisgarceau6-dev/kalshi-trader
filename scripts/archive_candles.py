#!/usr/bin/env python3
"""Archive yesterday's settled 15M markets + 1-min candles to data/candles/.

WHY THIS EXISTS
---------------
Kalshi retains settled markets for roughly 67 days. Every strategy question this
project has tried to answer has been blocked by that one fact: by the time a
hypothesis is formed, the only data that exists is the data it was formed on, and
the out-of-sample window is whatever few days sit outside the discovery set.
The NO-side question sat unresolved for months for exactly this reason — the
standing re-entry gate asked for 90 days of clean holdout that the API can never
supply.

Running this nightly turns that around: every day archived today is untouched
out-of-sample data for every hypothesis formed after today. It costs one workflow
run and ~120KB gzipped per day.

Output: data/candles/YYYY-MM-DD.csv.gz, schema-compatible with
backtest_ablation_raw.csv (minus the Coinbase spot columns).

Usage:
    python3 scripts/archive_candles.py               # yesterday (UTC)
    python3 scripts/archive_candles.py 2026-08-15    # a specific date
    python3 scripts/archive_candles.py --backfill 7  # last 7 days, skip existing
"""
import argparse
from decimal import Decimal, InvalidOperation
import base64
import csv
import gzip
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ensure_key():
    """CI passes the key as base64 PEM *content* in KALSHI_PRIVATE_KEY, but
    kalshi_auth.load_private_key() only reads a file path. Materialise it, same
    as daily_summary.py. Without this the nightly job fails on every run."""
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        return
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return
    p = Path("/tmp/kalshi_archive_key.pem")
    b64 = raw.replace("\n", "").replace("\r", "").replace(" ", "")
    b64 += "=" * (-len(b64) % 4)
    p.write_bytes(base64.b64decode(b64))
    p.chmod(0o600)
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(p)


_ensure_key()
from kalshi_auth import get as kalshi_get  # noqa: E402  (must follow _ensure_key)

# Gold and Silver added 2026-08-19: they were skipped as too thin, but Gold now trades
# MORE than ETH (median 92,518 contracts/market vs 81,877) and Silver sits between SOL
# and WTI, with zero dead markets across 200 settled each. Archiving is not trading —
# it only buys the option to backtest them, and Kalshi drops settled markets after ~67
# days, so every unarchived day is validation capacity destroyed permanently.
SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M",
               "KXBNB15M", "KXXRP15M", "KXWTI15M", "KXGOLD15M", "KXSILVER15M"]
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "candles")
FIELDS = ["series", "ticker", "close_ts", "close_hour", "candle_idx", "side", "ask",
          "secs_left", "won", "prior_1", "prior_2", "prior_3", "floor_strike"]

# Match the collection bounds of backtest_ablation_raw.csv so archived days can be
# concatenated with it directly. Deliberately wider than the live entry gates.
ASK_MIN, ASK_MAX = 88, 96
SECS_MIN, SECS_MAX = 100, 800


# ---------------------------------------------------------------------------
# WIDE ARCHIVE (added 2026-08-25) -- validation capacity for strategies that do
# not look like late-certainty.
#
# Everything above this block is shaped around ONE strategy: nine series, ask
# 88-96c, 100-800s to close. That is correct for what it feeds and useless for
# anything else -- a search for a different mechanism cannot be run on data
# filtered by the old mechanism's entry gates. Meanwhile Kalshi drops settled
# markets at ~67 days, so every day this is not collected is a day no future
# hypothesis can ever be tested against. On 2026-08-25 a strategy-2 search ended
# with "nothing survived" partly because the only 15M history available was
# ~8 days deep.
#
# This writes a SEPARATE file under data/candles/wide/. It is deliberately
# additive and cannot disturb the narrow archive:
#   - different directory, different field set, written after the narrow file
#   - scripts/backtest.py globs data/candles/*.csv.gz NON-recursively, so it
#     does not see these files at all
#   - the workflow's `git add data/candles` picks the subdirectory up with no
#     change to .github/workflows/
#   - FAIL-SOFT: any error here is caught and logged. The narrow archive is what
#     the live research depends on and must never be blocked by this.
#
# Rows are the full price path, no band filter and no entry-window filter --
# that is the entire point. YES-side quotes only; the NO book is the exact
# mirror and storing it doubles the file for no information. Mirror rule, which
# has been got wrong before (v5.16 orderbook bug), is:
#       no_bid = 100 - yes_ask      no_ask = 100 - yes_bid
# and inverting a range swaps high and low. Use wide_load() rather than
# re-deriving it by hand.
WIDE_DIR = os.path.join(OUT_DIR, "wide")

# Chosen for CLOSE CLUSTERS PER DAY, which is the currency of validation, not for
# headline volume -- a daily series yields one independent observation per day no
# matter how many strike-minutes it contains, and weather is the standing proof
# that volume and edge are unrelated here. The 15M block is every one that
# exists; the rest are categories that have never been deep-tested at all.
WIDE_SERIES = [
    # 15M -- 96 events/day each, the only archetype where an edge can be
    # confirmed or killed inside a couple of months.
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M",
    "KXWTI15M", "KXGOLD15M", "KXSILVER15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M",
    # Entertainment -- resolves on PUBLIC data that updates on a schedule
    # (rankings, charts). Never deep-tested, and the one category where the edge
    # would be reading the source faster than the market reprices rather than
    # anything in the order book. Few markets/day, so nearly free to archive.
    "KXNETFLIXRANKSHOW", "KXNETFLIXRANKMOVIE", "KXNETFLIXRANKMOVIERUNNERUP",
    "KXNETFLIXRANKSHOWRUNNERUP", "KXALBUMEQUIV", "KXPUREALBUMS", "KXYTVIEWSW",
    # Economics / politics -- scheduled releases, also never deep-tested.
    "KXAAAGASD", "KXAAAGASW", "KXAPRPOTUS", "KXTRUMPACT", "KXTRUTHSOCIAL",
]

# Lookback and resolution BY SETTLEMENT FREQUENCY. A fixed window is wrong in
# both directions: two hours is most of a 15M market's life and is nothing at all
# for a daily one, where by T-2h the outcome is effectively known and every quote
# sits at an extreme. Coarser sampling on longer markets keeps the file small
# without losing the part any strategy would live in.
# period_interval accepts only 1 (minute) and 60 (hour). Anything else returns
# HTTP 400 "Parameter validation failed", which reads as "this market has no data"
# and silently produces an archive containing nothing but the 15M series. Verified
# live: interval=10 -> 400, interval=1 and interval=60 -> 200.
WIDE_WINDOW = {
    "fifteen_min": (1800, 1),       # whole life of the market, minute bars
    "hourly":      (7200, 1),
    "daily":       (86400, 60),     # 24 hourly bars
    "weekly":      (604800, 60),
}
WIDE_DEFAULT = (86400, 60)

# Guard rails. Without them this is a nightly job that can hang: KXBTCD was dropped
# from the list above after fetch_markets spent >20 minutes paging its settled ladder
# for a single day. A per-series cap bounds any one series, and the budget bounds the
# whole job. A series is written COMPLETE or not at all, so hitting the budget leaves
# a clean missing-series gap rather than a silently truncated day.
WIDE_MAX_MARKETS_PER_SERIES = 250
WIDE_BUDGET_SECONDS = 2400          # 40 min; the narrow archive has already been written

# Closing quote + trade OHLC. Note what this CANNOT support: a maker fill model needs
# the bid's own high and low within the bar (a resting bid at B filled iff the book
# traded through it), and only the closing bid is kept here. Comparing px_low to
# yes_bid across a bar is therefore meaningless — the bid moved during it. If passive
# strategies are ever revisited, re-pull with research/search2/pull_ohlc.py, which
# stores full bid/ask OHLC. Kept narrower here because it is a nightly TRACKED file.
WIDE_FIELDS = ["series", "ticker", "close_ts", "ts", "secs_left",
               "yes_bid", "yes_ask", "px_close", "px_low", "px_high",
               "volume", "open_interest", "won", "strike"]


def _wide_window(series):
    if series.endswith("15M"):
        return WIDE_WINDOW["fifteen_min"]
    if series in ("KXBTCD", "KXINXU"):
        return WIDE_WINDOW["hourly"]
    if series.endswith("W"):
        return WIDE_WINDOW["weekly"]
    return WIDE_DEFAULT


def wide_fetch_markets(series, day_start, day_end):
    """Settled markets closing in [day_start, day_end), by close-time filter.

    fetch_markets() above walks the settled cursor until it sees a market older than
    the day. For a 15M series that is one page; for a low-frequency series whose
    settled list is not ordered helpfully it is the entire history — KXALBUMEQUIV
    took **15 minutes to find 27 markets** that way. `/markets` accepts min_close_ts
    and max_close_ts, which returns the same day in **0.2s and one page**.

    Deliberately NOT retrofitted onto fetch_markets(): that feeds the narrow archive
    the live research depends on, and changing how it selects markets is not a change
    to make as a side effect of adding a research feature. It is worth doing on its
    own, separately, with the two comparedout on the same days.
    """
    keep, cursor = [], None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200,
             "min_close_ts": day_start, "max_close_ts": day_end}
        if cursor:
            p["cursor"] = cursor
        try:
            code, r = kalshi_get("/markets", p)
        except Exception:
            time.sleep(2)
            continue
        if code != 200:
            break
        batch = r.get("markets", [])
        if not batch:
            break
        for m in batch:
            cts = parse_close_ts(m)
            # The bounds are advisory — a request for one UTC day comes back with a
            # few markets from the next one — so the day filter still has to be applied.
            if cts and day_start <= cts < day_end:
                keep.append(m)
        cursor = r.get("cursor")
        if not cursor:
            break
        time.sleep(0.05)
    return keep


def _num(d, key):
    try:
        return f'{float(d[f"{key}_dollars"]) * 100:.4f}'
    except (KeyError, TypeError, ValueError):
        return ""


def wide_rows_for_market(m, series):
    """Full price path for one market. Returns (rows, failed)."""
    ticker = m.get("ticker")
    close_ts = parse_close_ts(m)
    result = m.get("result")
    if not ticker or close_ts is None or result not in ("yes", "no"):
        return [], 0
    lookback, interval = _wide_window(series)
    # Same retry ladder as fetch_candles(): Kalshi 429s and resets connections under
    # a sustained pull, and a failure here must not be mistaken for an empty market.
    # Retry ONLY what is actually transient: 429 and network exceptions. Any other
    # non-200 means this market has no candle data, which is a real and common state
    # for low-frequency series — retrying it six times with exponential backoff costs
    # ~32s per market and produces nothing. That mistake made one entertainment series
    # take 15 minutes to return zero rows.
    cs = None
    for attempt in range(5):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - lookback, "end_ts": close_ts,
                 "period_interval": interval})
            if code == 200:
                cs = r.get("candlesticks", [])
                break
            if code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            return [], 0          # no data for this market; not a failure
        except Exception:
            time.sleep(min(2 ** attempt, 8))
    if cs is None:
        return [], 1
    out = []
    for c in cs:
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        bid, ask = _num(c.get("yes_bid", {}), "close"), _num(c.get("yes_ask", {}), "close")
        if not bid or not ask:
            continue
        px = c.get("price") or {}
        out.append({
            "series": series, "ticker": ticker, "close_ts": close_ts, "ts": ts,
            "secs_left": close_ts - ts, "yes_bid": bid, "yes_ask": ask,
            "px_close": _num(px, "close"), "px_low": _num(px, "low"),
            "px_high": _num(px, "high"),
            "volume": c.get("volume") or c.get("volume_fp") or 0,
            "open_interest": c.get("open_interest") or c.get("open_interest_fp") or 0,
            "won": result == "yes", "strike": m.get("floor_strike"),
        })
    return out, 0


def archive_day_wide(date_str, start, end, force=False):
    """Additive wide archive. Never raises -- the narrow archive comes first."""
    try:
        os.makedirs(WIDE_DIR, exist_ok=True)
        path = os.path.join(WIDE_DIR, f"{date_str}.csv.gz")
        if os.path.exists(path) and not force:
            print(f"  wide: {date_str} already archived, skipping")
            return
        rows, failures, seen, skipped = [], 0, 0, []
        t0 = time.time()
        for series in WIDE_SERIES:
            if time.time() - t0 > WIDE_BUDGET_SECONDS:
                skipped.append(series)
                continue
            ms = wide_fetch_markets(series, start, end)
            if len(ms) > WIDE_MAX_MARKETS_PER_SERIES:
                print(f"    wide {series}: {len(ms)} markets exceeds the "
                      f"{WIDE_MAX_MARKETS_PER_SERIES} cap — skipping whole series",
                      flush=True)
                skipped.append(series)
                continue
            seen += len(ms)
            n0 = len(rows)
            for m in ms:
                r, f = wide_rows_for_market(m, series)
                rows += r
                failures += f
                time.sleep(0.02)
            # Per-series progress. A nightly job that prints nothing for half an hour
            # cannot be diagnosed from a CI log, and this one walks ~25 series of very
            # different sizes -- when it runs long the log has to say which one.
            print(f"    wide {series}: {len(ms)} markets, {len(rows)-n0:,} rows, "
                  f"{time.time()-t0:.0f}s cumulative", flush=True)
        if not rows:
            print(f"  wide: {date_str} produced no rows ({seen} markets)")
            return
        # Same anti-truncation guard as the narrow archive: a --force refetch of an
        # aged-out day legitimately returns less, and overwriting then destroys data
        # that cannot be re-fetched at any price.
        if os.path.exists(path):
            try:
                with gzip.open(path, "rt") as f:
                    existing = sum(1 for _ in csv.DictReader(f))
            except OSError:
                existing = 0
            if existing and len(rows) < existing * 0.95:
                print(f"  wide: ABORT {date_str} — refetch {len(rows):,} rows vs "
                      f"{existing:,} on disk; refusing to shrink the archive")
                return
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", newline="") as f:
            w = csv.DictWriter(f, fieldnames=WIDE_FIELDS)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
        note = f", {failures} fetch failures" if failures else ""
        if skipped:
            note += f", SKIPPED {len(skipped)}: {','.join(skipped)}"
        print(f"  wide: {date_str} wrote {len(rows):,} rows from {seen} markets "
              f"-> {path} ({os.path.getsize(path)/1024:.0f} KB){note}")
    except Exception as e:                       # noqa: BLE001 -- must never propagate
        print(f"  wide: FAILED for {date_str}: {type(e).__name__}: {e}")


def parse_close_ts(m):
    ct = m.get("close_time", "")
    if not ct:
        return 0
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def candle_price(c, side):
    """Ask in EXACT cents for the given side. NO ask = 100 - YES bid.

    Kalshi quotes sub-cent: `close_dollars` carries four decimal places, so 0.9330 is
    a real ask of 93.30c. This used to round to integer cents, which silently
    manufactured false candidates — a 93.3000c ask became 93c, which is inside the
    live [90,93] band when the true price is not. An audit on 2026-08-22 probed 180
    markets and found rounding changed 18.4% of selected identities. Every capture
    figure computed against this archive inherited that error in its denominator.

    Returns a Decimal so the value is exact, never a float that prints as 93.30000001.
    Rows written before 2026-08-22 hold integer cents and cannot be recovered by
    parsing; re-archive a day with --force to upgrade it in place.
    """
    try:
        if side == "yes":
            return Decimal(str(c["yes_ask"]["close_dollars"])) * 100
        yes_bid = Decimal(str(c["yes_bid"]["close_dollars"])) * 100
        return Decimal(100) - yes_bid if yes_bid > 0 else None
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


def fetch_markets(series, day_start, day_end):
    """Settled markets closing inside [day_start, day_end)."""
    keep, cursor = [], None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        try:
            code, r = kalshi_get("/markets", p)
        except Exception:
            time.sleep(2)
            continue
        if code != 200:
            break
        batch = r.get("markets", [])
        if not batch:
            break
        stop = False
        for m in batch:
            cts = parse_close_ts(m)
            if not cts:
                continue
            if cts < day_start:
                stop = True
                break
            if cts < day_end:
                keep.append(m)
        cursor = r.get("cursor")
        if stop or not cursor:
            break
        time.sleep(0.05)
    return keep


def fetch_candles(series, ticker, close_ts):
    for a in range(6):
        try:
            code, r = kalshi_get(
                f"/series/{series}/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 900, "end_ts": close_ts + 10,
                 "period_interval": 1})
            if code == 200:
                return sorted(r.get("candlesticks", []),
                              key=lambda c: c.get("end_period_ts", 0))
            if code == 429:
                time.sleep(1.5 * (a + 1))
                continue
        except Exception:
            pass
        time.sleep(min(2 ** a, 8))
    return None


def rows_for_market(m, series):
    ticker, result = m.get("ticker", ""), m.get("result", "")
    close_ts = parse_close_ts(m)
    if not result or not close_ts:
        return [], False
    candles = fetch_candles(series, ticker, close_ts)
    if candles is None:
        return [], True                      # signal a fetch failure
    strike = float(m.get("floor_strike") or 0)
    hour = datetime.fromtimestamp(close_ts, tz=timezone.utc).hour
    out = []
    for i, c in enumerate(candles):
        secs_left = close_ts - c.get("end_period_ts", 0)
        if not (SECS_MIN <= secs_left <= SECS_MAX):
            continue
        for side in ("yes", "no"):
            ask = candle_price(c, side)
            if ask is None or not (ASK_MIN <= ask <= ASK_MAX):
                continue
            pc = lambda o: candle_price(candles[i - o], side) if i - o >= 0 else None
            out.append({
                "series": series, "ticker": ticker, "close_ts": close_ts,
                "close_hour": hour, "candle_idx": i, "side": side, "ask": ask,
                "secs_left": int(secs_left), "won": result == side,
                "prior_1": pc(1) if pc(1) is not None else "",
                "prior_2": pc(2) if pc(2) is not None else "",
                "prior_3": pc(3) if pc(3) is not None else "",
                "floor_strike": strike,
            })
    return out, False


def archive_day(date_str, force=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{date_str}.csv.gz")
    if os.path.exists(path) and not force:
        print(f"{date_str}: already archived, skipping")
        return True

    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start, end = int(day.timestamp()), int((day + timedelta(days=1)).timestamp())
    if end > time.time():
        print(f"{date_str}: day not finished yet, refusing to archive a partial day")
        return False

    all_rows, failures, markets_seen = [], 0, 0
    for series in SERIES_LIST:
        ms = fetch_markets(series, start, end)
        markets_seen += len(ms)
        for m in ms:
            rows, failed = rows_for_market(m, series)
            all_rows += rows
            failures += failed
            time.sleep(0.08)
        print(f"  {series}: {len(ms)} markets", flush=True)

    if failures:
        # A partial day is worse than no day — it would look like a real gap later.
        print(f"{date_str}: ABORT — {failures} candle fetches failed; not writing "
              f"a partial archive. Re-run to retry.")
        return False
    if not all_rows:
        print(f"{date_str}: no qualifying rows ({markets_seen} markets scanned)")
        return False

    # Kalshi drops settled markets at ~67 days, so a --force re-archive of an old day
    # can legitimately return far less than the file already holds. Overwriting then
    # destroys data that cannot be re-fetched at any price. Refuse to shrink a healthy
    # archive; --force is for upgrading precision, not for truncating history.
    if os.path.exists(path):
        try:
            with gzip.open(path, "rt") as f:
                existing = sum(1 for _ in csv.DictReader(f))
        except OSError:
            existing = 0
        if existing and len(all_rows) < existing * 0.95:
            print(f"{date_str}: ABORT — refetch has {len(all_rows):,} rows but the "
                  f"existing archive has {existing:,}. Kalshi has likely aged this day "
                  f"out; refusing to overwrite good data with a thinner copy.")
            return False

    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    os.replace(tmp, path)                    # atomic: never leave a half file
    print(f"{date_str}: wrote {len(all_rows):,} rows from {markets_seen} markets "
          f"-> {path} ({os.path.getsize(path)/1024:.0f} KB)")

    # Wide archive LAST and fail-soft. The narrow file above is what the live
    # research depends on; it is already written and this cannot take it back.
    if not os.environ.get("SKIP_WIDE_ARCHIVE"):
        archive_day_wide(date_str, start, end, force)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD (default: yesterday UTC)")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="archive the last N complete days, skipping existing")
    ap.add_argument("--force", action="store_true", help="overwrite existing archive")
    a = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    if a.backfill:
        ok = True
        for i in range(a.backfill, 0, -1):
            ok &= archive_day(str(today - timedelta(days=i)), a.force)
        return 0 if ok else 1
    day = a.date or str(today - timedelta(days=1))
    return 0 if archive_day(day, a.force) else 1


if __name__ == "__main__":
    sys.exit(main())
