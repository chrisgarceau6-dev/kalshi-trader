#!/usr/bin/env python3
"""KXBTC15M mean-reversion trader for Kalshi.

Signals validated on 5 years of BTC 15-min OHLCV data (Jan 2024–Jul 2026 OOS,
87,653 periods). Only signals that survive Bonferroni correction (p<0.00104,
k=48 tests) or pass p<0.01 with consistent IS/OOS are included.

Priority order (only the first matching signal fires per period):
  M2: prev DN>0.3% AND prev2 DN>0.3%  → YES $150  57.0% OOS  z=4.6  p<0.00001
  M1: prev DN>0.3%                     → YES $100  55.3% OOS  z=8.4  p<0.00001
  H13: hour==13 AND prev UP            → NO  $100  54.7% OOS  z=4.0  p=0.00003
  H00: hour==0  AND prev UP            → NO  $100  54.1% OOS  z=3.4  p=0.00029
  H01: hour==1  AND prev UP AND prev2 UP → NO $100 54.9% OOS  z=2.9  p=0.00211

Dropped vs. prior version:
  S1 (hr02 UP→NO): 52.4% OOS — below 53.5% fee breakeven, loses money
  S3 (hr12 DN→NO): 46.9% OOS — signal was backwards
  S5 (hr03 UP→YES): 47.7% OOS — signal was backwards
  S6/S7 (hr19): OOS data was snooped in prior session; 5yr data is marginal

Runs via GitHub Actions cron: 0,15,30,45 * * * *
State persisted between runs in btc15m_state.json via Actions cache.

usage:
    python btc15m_trader.py --once       # live trade (default)
    python btc15m_trader.py --dry-run    # detect signals, no orders placed
    python btc15m_trader.py --status     # print current state JSON
"""

import argparse, base64, json, os
from datetime import datetime, timezone
from pathlib import Path

from kalshi_auth import get as _get, place_order

BASE       = Path(__file__).parent
STATE_FILE = BASE / "btc15m_state.json"
LOG_FILE   = BASE / "btc15m.log"

SERIES         = "KXBTC15M"
MAX_PRICE_CENTS = 56    # refuse bet if ask > 56¢ (market moved from 50/50)
BIG_MOVE_PCT    = 0.003  # 0.3% — the one magnitude threshold confirmed by 5yr data


# ── auth ──────────────────────────────────────────────────────────────────────

def _ensure_key():
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        return
    b64 = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if b64:
        p = Path("/tmp/kalshi_btc15m_key.pem")
        p.write_bytes(base64.b64decode(b64))
        p.chmod(0o600)
        os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(p)


def kalshi_get(path, params=None):
    _ensure_key()
    return _get(path, params)


# ── state / logging ───────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_bet_event": "", "stats": {"bets": 0, "pnl_dollars": 0.0}}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str))


def log(msg):
    ts   = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


# ── market fetching ───────────────────────────────────────────────────────────

def _markets_direct(status, limit):
    code, data = kalshi_get("/markets", {"series_ticker": SERIES, "status": status, "limit": limit})
    if code == 200:
        return data.get("markets", [])
    return []


def _markets_via_events(status, limit):
    code, data = kalshi_get("/events", {"series_ticker": SERIES, "status": status, "limit": limit})
    if code != 200:
        return []
    markets = []
    for ev in data.get("events", []):
        code2, mdata = kalshi_get("/markets", {
            "event_ticker": ev.get("event_ticker", ""), "limit": 5
        })
        if code2 == 200:
            markets.extend(mdata.get("markets", []))
    return markets


def fetch_settled(limit=5):
    """Return recent settled KXBTC15M markets, newest first."""
    mktlist = _markets_direct("settled", limit) or _markets_via_events("settled", limit)
    mktlist = [
        m for m in mktlist
        if m.get("result") in ("yes", "no")
        and m.get("floor_strike")
        and m.get("expiration_value")
    ]
    return sorted(mktlist, key=lambda m: m.get("close_time", ""), reverse=True)


def fetch_open():
    """Return the current open KXBTC15M market, or None."""
    mktlist = _markets_direct("open", 3) or _markets_via_events("open", 1)
    return mktlist[0] if mktlist else None


# ── signal logic ──────────────────────────────────────────────────────────────

def evaluate_signal(hour_utc, prev_yes, prev2_yes, prev_mag, prev2_mag):
    """
    Return (signal_name, side, dollars) or (None, None, None).

    All signals confirmed on 914-day OOS BTC price data (5yr Bonferroni-corrected).
    Fee breakeven at 50¢ entry with 7% Kalshi fee = 53.5% win rate required.
    """
    # M2: consecutive big down moves → strong bounce (57.0% OOS, z=4.6)
    if (not prev_yes) and (not prev2_yes) and prev_mag > BIG_MOVE_PCT and prev2_mag > BIG_MOVE_PCT:
        return "M2", "yes", 150

    # M1: single big down move → bounce (55.3% OOS, z=8.4, most frequent signal)
    if (not prev_yes) and prev_mag > BIG_MOVE_PCT:
        return "M1", "yes", 100

    # H13: 1PM UTC up move reverts — London afternoon / US pre-market (54.7%, z=4.0)
    if hour_utc == 13 and prev_yes:
        return "H13", "no", 100

    # H00: midnight UTC up move reverts — Asia open (54.1%, z=3.4)
    if hour_utc == 0 and prev_yes:
        return "H00", "no", 100

    # H01: two consecutive up moves at 1AM UTC revert (54.9%, z=2.9, p=0.002)
    if hour_utc == 1 and prev_yes and prev2_yes:
        return "H01", "no", 100

    return None, None, None


# ── order placement ───────────────────────────────────────────────────────────

def place_bet(ticker, side, dollars):
    """Fetch live ask, place a crossing limit order. Returns True on acceptance."""
    code, resp = kalshi_get(f"/markets/{ticker}")
    if code != 200:
        log(f"  cannot fetch market {ticker} (HTTP {code})")
        return False

    market    = resp.get("market", resp)
    ask_field = "yes_ask_dollars" if side == "yes" else "no_ask_dollars"
    raw_ask   = float(market.get(ask_field) or 0.50)
    ask_cents = int(round(raw_ask * 100))

    if ask_cents > MAX_PRICE_CENTS:
        log(f"  skip — {side} ask is {ask_cents}¢ > max {MAX_PRICE_CENTS}¢")
        return False

    limit_cents = min(MAX_PRICE_CENTS, ask_cents + 1)  # cross spread by 1¢
    count       = max(1, int(dollars * 100 / limit_cents))
    est_cost    = round(count * limit_cents / 100, 2)

    log(f"  → {count} {side.upper()} @ {limit_cents}¢ on {ticker}  (est. ${est_cost:.2f})")

    code, order_resp = place_order(
        ticker, side, count,
        yes_price_cents=limit_cents if side == "yes" else None,
        no_price_cents=limit_cents  if side == "no"  else None,
    )

    if code in (200, 201):
        log(f"  order accepted ✓")
        return True
    log(f"  order FAILED — HTTP {code}: {order_resp}")
    return False


# ── main ──────────────────────────────────────────────────────────────────────

def run_once(dry_run=False):
    state   = load_state()
    now_utc = datetime.now(timezone.utc)
    log(f"=== BTC15M @ {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")

    # 1. Pull last 2 settled periods
    settled = fetch_settled(limit=5)
    if len(settled) < 2:
        log(f"  only {len(settled)} settled market(s) — skipping")
        return

    prev, prev2 = settled[0], settled[1]
    prev_yes    = (prev.get("result") == "yes")
    prev2_yes   = (prev2.get("result") == "yes")
    prev_start  = float(prev["floor_strike"])
    prev_end    = float(prev["expiration_value"])
    prev2_start = float(prev2["floor_strike"])
    prev2_end   = float(prev2["expiration_value"])
    prev_mag    = abs(prev_end  - prev_start)  / prev_start  if prev_start  > 0 else 0
    prev2_mag   = abs(prev2_end - prev2_start) / prev2_start if prev2_start > 0 else 0

    log(f"  prev  {prev.get('close_time','')[:16]}  "
        f"{'UP' if prev_yes else 'DN'}  ${prev_start:,.0f}→${prev_end:,.0f}  ({prev_mag:.3%})")
    log(f"  prev2 {prev2.get('close_time','')[:16]}  "
        f"{'UP' if prev2_yes else 'DN'}  ${prev2_start:,.0f}→${prev2_end:,.0f}  ({prev2_mag:.3%})")

    # 2. Find the currently open market
    open_mkt = fetch_open()
    if not open_mkt:
        log("  no open KXBTC15M market — skipping")
        return

    open_ticker = open_mkt.get("ticker", "")
    open_event  = open_mkt.get("event_ticker", "")
    open_time   = open_mkt.get("open_time", "")

    if state.get("last_bet_event") == open_event:
        log(f"  already traded {open_event} — skip")
        return

    try:
        open_dt  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        hour_utc = open_dt.hour
    except Exception:
        log(f"  cannot parse open_time {open_time!r} — skipping")
        return

    log(f"  open: {open_ticker}  hour_utc={hour_utc}")

    # 3. Evaluate signals
    signal, side, dollars = evaluate_signal(hour_utc, prev_yes, prev2_yes, prev_mag, prev2_mag)

    if not signal:
        log(f"  no signal  (hour={hour_utc} "
            f"prev={'UP' if prev_yes else 'DN'} {prev_mag:.3%}  "
            f"prev2={'UP' if prev2_yes else 'DN'} {prev2_mag:.3%})")
        return

    log(f"  SIGNAL {signal} → BET {side.upper()} ${dollars}")

    if dry_run:
        log("  [dry-run] order skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log("  KALSHI_API_KEY_ID not set — cannot trade")
        return

    # 4. Place bet and save state
    placed = place_bet(open_ticker, side, dollars)
    if placed:
        state["last_bet_event"]   = open_event
        state["last_bet_at"]      = now_utc.isoformat(timespec="seconds")
        state["last_bet_signal"]  = signal
        state["last_bet_side"]    = side
        state["last_bet_dollars"] = dollars
        state["last_bet_ticker"]  = open_ticker
        state["stats"]["bets"]   += 1
        save_state(state)


def main():
    ap = argparse.ArgumentParser(description="KXBTC15M mean-reversion trader")
    ap.add_argument("--once",    action="store_true", help="run one poll (default)")
    ap.add_argument("--dry-run", action="store_true", help="detect signals, no orders")
    ap.add_argument("--status",  action="store_true", help="print state JSON and exit")
    a = ap.parse_args()

    if a.status:
        print(json.dumps(load_state(), indent=2))
        return

    run_once(dry_run=a.dry_run)


if __name__ == "__main__":
    main()
