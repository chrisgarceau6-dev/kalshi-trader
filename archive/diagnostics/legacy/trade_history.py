#!/usr/bin/env python3
"""
Kalshi trade history — v5.5 period (Aug 5+).
Computes P&L correctly: win = contracts * (1 - avg_price) * 0.93, loss = -cost
"""
import os, sys, time, json
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent

def _load_dotenv():
    env_file = BASE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

_load_dotenv()

sys.path.insert(0, str(BASE))
import kalshi_auth as K

FEE = 0.07
V55_START = "2026-08-05"  # v5.5 deployed Aug 5

# ── fetch all fills ──────────────────────────────────────────────────────────

def fetch_fills(min_date=V55_START):
    fills = []
    cursor = None
    page = 0
    while True:
        params = {"limit": 200}
        if cursor:
            # /portfolio/fills paginates on "cursor". It silently ignores
            # "page_cursor" and re-serves page 1, so the wrong name here is an
            # infinite loop, not an error.
            params["cursor"] = cursor
        code, data = K.get("/portfolio/fills", params=params)
        if code != 200:
            print(f"fills error {code}: {data}")
            break
        batch = data.get("fills", [])
        if not batch:
            break
        # fills are newest-first; stop when we hit pre-cutoff
        stop = False
        for f in batch:
            ts = f.get("created_time", "")
            if ts[:10] < min_date:
                stop = True
                break
            fills.append(f)
        page += 1
        if page % 5 == 0:
            print(f"  {len(fills)} fills fetched...", end="\r")
        cursor = data.get("cursor")
        if not cursor or stop:
            break
        time.sleep(0.15)
    return fills

# ── aggregate fills into positions ──────────────────────────────────────────

def aggregate(fills):
    """Group fills by ticker. Returns dict: ticker → {side, contracts, cost, series}."""
    pos = {}
    for f in fills:
        ticker  = f["ticker"]
        side    = f["side"]     # "yes" or "no"
        action  = f["action"]   # "buy" or "sell" (sell = opening short; usually "buy")
        yp      = float(f["yes_price"])
        count   = int(f["count"])
        # price from the buyer's perspective
        price   = yp if side == "yes" else (1.0 - yp)

        if ticker not in pos:
            series = "-".join(ticker.split("-")[:1]) if "-" in ticker else ticker
            # extract series prefix (e.g. KXBTC15M from KXBTC15M-26AUG081045-T98000)
            parts = ticker.split("-")
            series = parts[0] if parts else ticker
            pos[ticker] = {"side": side, "contracts": 0, "cost": 0.0, "series": series}

        if action == "buy":
            pos[ticker]["contracts"] += count
            pos[ticker]["cost"]      += price * count
        else:
            pos[ticker]["contracts"] -= count
            pos[ticker]["cost"]      -= price * count

    # remove zero-contract (fully closed) positions
    return {t: v for t, v in pos.items() if v["contracts"] > 0}

# ── fetch market results ─────────────────────────────────────────────────────

def fetch_result(ticker):
    """Return 'yes', 'no', or None (still open/void)."""
    code, data = K.get(f"/markets/{ticker}")
    if code != 200:
        return None
    mkt = data.get("market", data)
    result = mkt.get("result", None)
    status = mkt.get("status", "")
    if status not in ("settled", "determined"):
        return None
    return result.lower() if result else None

# ── P&L formula ──────────────────────────────────────────────────────────────

def compute_pnl(pos, result):
    """
    pos: {side, contracts, cost}
    result: "yes" or "no"
    Returns net P&L in dollars.
    """
    n    = pos["contracts"]
    cost = pos["cost"]
    side = pos["side"]
    avg_price = cost / n if n else 0.0

    win = (side == result)
    if win:
        # collect $1 per contract, fee is 7% on profit only
        profit_per = 1.0 - avg_price
        return n * profit_per * (1 - FEE)
    else:
        return -cost

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Fetching fills since {V55_START}...")
    fills = fetch_fills(V55_START)
    print(f"{len(fills)} fills")

    if not fills:
        print("No fills found.")
        return

    positions = aggregate(fills)
    print(f"{len(positions)} open/settled positions across {len(set(p['series'] for p in positions.values()))} series")

    # per-series accumulators
    series_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})

    settled = 0
    pending = 0
    total_pnl = 0.0
    skipped = 0

    print("Fetching market results...")
    tickers = list(positions.keys())
    for i, ticker in enumerate(tickers):
        if i and i % 50 == 0:
            print(f"  {i}/{len(tickers)}", end="\r")
        pos = positions[ticker]
        result = fetch_result(ticker)
        time.sleep(0.05)

        if result is None:
            pending += 1
            continue

        pnl = compute_pnl(pos, result)
        win = (pos["side"] == result)
        s   = pos["series"]

        series_stats[s]["n"]    += 1
        series_stats[s]["wins"] += 1 if win else 0
        series_stats[s]["pnl"]  += pnl
        total_pnl += pnl
        settled   += 1

    total_n   = sum(v["n"]    for v in series_stats.values())
    total_win = sum(v["wins"] for v in series_stats.values())
    total_wr  = total_win / total_n * 100 if total_n else 0

    print(f"\n{'Series':<30}  {'n':>5}  {'WR':>7}  {'Net P&L':>10}")
    print("-" * 58)
    for s in sorted(series_stats):
        v  = series_stats[s]
        wr = v["wins"] / v["n"] * 100 if v["n"] else 0
        print(f"  {s:<28}  {v['n']:>5}  {wr:>6.1f}%  ${v['pnl']:>+9.2f}")
    print("=" * 58)
    print(f"  {'TOTAL':<28}  {total_n:>5}  {total_wr:>6.1f}%  ${total_pnl:>+9.2f}")
    print(f"\nSettled: {settled} | Pending: {pending} | Skipped: {skipped}")

if __name__ == "__main__":
    main()
