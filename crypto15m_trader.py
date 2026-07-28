#!/usr/bin/env python3
"""Multi-asset 15-min Kalshi trader — direction-neutral signals.

Strategy:
  1. XARB — Cross-series price arbitrage.  ETH is the most liquid anchor;
     it price-discovers first.  When ETH's YES ask diverges from a follower's
     YES ask by >= XARB_THRESH_CENTS and ETH is > XARB_ETH_MIN_AWAY from 50c,
     the follower is mispriced relative to the 83%+ co-resolution rate.
     We bet the follower toward ETH.  No view on crypto direction required.

  2. CALENDAR — Pure hour-of-day bias validated on 69-day Kalshi data (z > 2).
     Fires at fixed UTC hours regardless of recent price action.

usage: --once | --dry-run | --status
"""

import argparse, base64, json, os, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from kalshi_auth import get as _get, place_order

BASE       = Path(__file__).parent
STATE_FILE = BASE / "crypto15m_state.json"
LOG_FILE   = BASE / "crypto15m.log"

# ── constants ──────────────────────────────────────────────────────────────────

XARB_THRESH_CENTS = 8   # min ETH-vs-follower YES gap to trigger arb
XARB_ETH_MIN_AWAY = 7   # ETH must be >= N cents from 50c to be informative

XARB_KELLY     = 0.025  # fractional Kelly for XARB bets
CALENDAR_KELLY = 0.020  # fractional Kelly for calendar bets
MIN_BET        = 10
MAX_BET        = 30
MAX_PRICE_CENTS = 56    # skip if our side costs more than this

# (series, hour_utc) -> (signal_name, side)
# z-scores from 69-day backtest noted in comments
CALENDAR = {
    ("KXETH15M",  15): ("HB15", "yes"),   # ETH h=15 positive bias
    ("KXETH15M",  22): ("HB22", "no"),    # ETH h=22 negative bias  (z=-2.53)
    ("KXDOGE15M", 22): ("HB22", "no"),    # DOGE h=22 negative bias (z=-2.05)
    ("KXSOL15M",   2): ("HB02", "no"),    # SOL h=2 negative bias
    ("KXBNB15M",   4): ("HB04", "no"),    # BNB h=4 negative bias   (z=-3.37)
    ("KXDOGE15M",  4): ("HB04", "no"),    # DOGE h=4 negative bias  (z=-2.05)
}

SERIES_CONFIG = {
    "KXETH15M":  {"anchor": True,  "xarb": False, "calendar": True},
    "KXSOL15M":  {"anchor": False, "xarb": True,  "calendar": True},
    "KXDOGE15M": {"anchor": False, "xarb": True,  "calendar": True},
    "KXBNB15M":  {"anchor": False, "xarb": True,  "calendar": True},
    "KXXRP15M":  {"anchor": False, "xarb": True,  "calendar": False},  # 79.7% ETH co-resolution
}


# ── infrastructure ─────────────────────────────────────────────────────────────

def _ensure_key():
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        return
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return
    p   = Path("/tmp/kalshi_crypto15m_key.pem")
    b64 = raw.replace("\n", "").replace("\r", "").replace(" ", "")
    b64 += "=" * (-len(b64) % 4)
    p.write_bytes(base64.b64decode(b64))
    p.chmod(0o600)
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(p)


def kalshi_get(path, params=None):
    _ensure_key()
    return _get(path, params)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"series": {}, "stats": {"bets": 0, "pnl_dollars": 0.0}}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str))


def log(msg):
    ts   = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def fetch_balance():
    code, resp = kalshi_get("/portfolio/balance")
    if code != 200:
        log(f"  balance fetch HTTP {code}: {str(resp)[:120]}")
        return None
    try:
        raw = resp.get("balance") or resp.get("available_balance_cents", 0)
        if raw:
            return float(raw) / 100
        dollars_str = resp.get("balance_dollars", "")
        if dollars_str:
            return float(dollars_str)
        return None
    except Exception as e:
        log(f"  balance parse error: {e} — resp={str(resp)[:120]}")
        return None


def send_email(subject, body):
    to_addr   = os.environ.get("COPY_EMAIL_TO", "")
    from_addr = os.environ.get("COPY_EMAIL_FROM", "")
    password  = os.environ.get("COPY_EMAIL_PASSWORD", "")
    if not (to_addr and from_addr and password):
        return
    try:
        msg            = MIMEText(body, "plain")
        msg["From"]    = from_addr
        msg["To"]      = to_addr
        msg["Subject"] = subject
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(from_addr, password)
            s.send_message(msg)
        log(f"  email sent: {subject}")
    except Exception as e:
        log(f"  email failed: {e}")


# ── market fetching ────────────────────────────────────────────────────────────

def _markets_direct(series, status, limit):
    code, data = kalshi_get("/markets", {"series_ticker": series, "status": status, "limit": limit})
    return data.get("markets", []) if code == 200 else []


def _markets_via_events(series, status, limit):
    code, data = kalshi_get("/events", {"series_ticker": series, "status": status, "limit": limit})
    if code != 200:
        return []
    markets = []
    for ev in data.get("events", []):
        code2, mdata = kalshi_get("/markets", {"event_ticker": ev.get("event_ticker", ""), "limit": 5})
        if code2 == 200:
            markets.extend(mdata.get("markets", []))
    return markets


def fetch_open(series):
    mktlist = _markets_direct(series, "open", 3) or _markets_via_events(series, "open", 1)
    return mktlist[0] if mktlist else None


def fetch_market_price(ticker):
    """Return (yes_cents, no_cents) from live orderbook, or (None, None)."""
    code, resp = kalshi_get(f"/markets/{ticker}")
    if code != 200:
        return None, None
    mkt = resp.get("market", resp)

    def _cents(val):
        try:
            return int(round(float(val) * 100)) if val is not None else None
        except Exception:
            return None

    yes_cents = _cents(mkt.get("yes_ask_dollars") or mkt.get("yes_ask"))
    no_cents  = _cents(mkt.get("no_ask_dollars")  or mkt.get("no_ask"))
    return yes_cents, no_cents


# ── signals ───────────────────────────────────────────────────────────────────

def eval_xarb(eth_yes_cents, series_yes_cents):
    """
    Cross-series price arb using ETH as anchor.
    ETH/follower co-resolve 83%+. When prices diverge, the follower is mispriced.
    Returns (signal, side) or (None, None).
    """
    if eth_yes_cents is None or series_yes_cents is None:
        return None, None
    if abs(eth_yes_cents - 50) < XARB_ETH_MIN_AWAY:
        return None, None  # ETH near 50c — not informative enough
    diff = eth_yes_cents - series_yes_cents
    if diff >= XARB_THRESH_CENTS:
        return "XARB_YES", "yes"   # ETH says UP more; follower YES is cheap
    if diff <= -XARB_THRESH_CENTS:
        return "XARB_NO", "no"    # ETH says DOWN more; follower NO is cheap
    return None, None


def eval_calendar(series, hour_utc):
    """Pure hour-of-day bias, no directional component."""
    return CALENDAR.get((series, hour_utc), (None, None))


# ── sizing ────────────────────────────────────────────────────────────────────

def size_bet(signal, balance):
    kelly = XARB_KELLY if signal.startswith("XARB") else CALENDAR_KELLY
    return max(MIN_BET, min(MAX_BET, int(balance * kelly)))


# ── order placement ───────────────────────────────────────────────────────────

def place_bet(ticker, side, dollars):
    """Return True=placed, False=API rejection, None=skipped (price/liquidity)."""
    code, resp = kalshi_get(f"/markets/{ticker}")
    if code != 200:
        log(f"  cannot fetch market {ticker} (HTTP {code})")
        return None

    market    = resp.get("market", resp)
    ask_field = "yes_ask_dollars" if side == "yes" else "no_ask_dollars"
    raw_ask   = market.get(ask_field) or market.get(ask_field.replace("_dollars", ""))

    if raw_ask is None:
        log(f"  skip — no {side} ask (no liquidity)")
        return None

    ask_cents = int(round(float(raw_ask) * 100))
    if ask_cents > MAX_PRICE_CENTS:
        log(f"  skip — {side} ask is {ask_cents}c > max {MAX_PRICE_CENTS}c")
        return None

    limit_cents = min(MAX_PRICE_CENTS, ask_cents + 1)
    count       = max(1, int(dollars * 100 / limit_cents))
    est_cost    = round(count * limit_cents / 100, 2)
    log(f"  -> {count} {side.upper()} @ {limit_cents}c  (est. ${est_cost:.2f})")

    code, order_resp = place_order(
        ticker, side, count,
        yes_price_cents=limit_cents if side == "yes" else None,
        no_price_cents=limit_cents  if side == "no"  else None,
    )
    if code in (200, 201):
        log(f"  order accepted")
        return True
    log(f"  order FAILED — HTTP {code}: {order_resp}")
    return False


# ── polling ───────────────────────────────────────────────────────────────────

def poll_series(series, config, state, now_utc, dry_run, balance, eth_yes_cents):
    series_state = state.setdefault("series", {}).setdefault(series, {"last_bet_event": ""})
    log(f"  [{series}]")

    open_mkt = fetch_open(series)
    if not open_mkt:
        log(f"    no open market — skipping")
        return

    open_ticker = open_mkt.get("ticker", "")
    open_event  = open_mkt.get("event_ticker", "")
    open_time   = open_mkt.get("open_time", "")
    close_time  = open_mkt.get("close_time", "")

    if series_state.get("last_bet_event") == open_event:
        log(f"    already traded {open_event} — skip")
        return

    try:
        open_dt  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        hour_utc = open_dt.hour
    except Exception:
        log(f"    cannot parse open_time {open_time!r} — skipping")
        return

    try:
        close_dt  = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        secs_left = (close_dt - now_utc).total_seconds()
        if secs_left < 240:
            log(f"    only {secs_left:.0f}s left — skip")
            return
        log(f"    {secs_left:.0f}s left  h={hour_utc}")
    except Exception:
        pass

    signal, side = None, None

    if config.get("xarb"):
        series_yes_cents, _ = fetch_market_price(open_ticker)
        log(f"    ETH={eth_yes_cents}c  {series}={series_yes_cents}c")
        signal, side = eval_xarb(eth_yes_cents, series_yes_cents)

    if not signal and config.get("calendar"):
        signal, side = eval_calendar(series, hour_utc)

    if not signal:
        log(f"    no signal (h={hour_utc})")
        return

    dollars = size_bet(signal, balance)
    log(f"    SIGNAL {signal} -> BET {side.upper()} ${dollars}  (balance=${balance:.0f})")

    if dry_run:
        log(f"    [dry-run] order skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log(f"    KALSHI_API_KEY_ID not set — cannot trade")
        return

    placed = place_bet(open_ticker, side, dollars)

    if placed is True:
        send_email(
            f"[Kalshi] Trade {series} {signal} {side.upper()} ${dollars}",
            f"Signal: {signal}\nSide: {side.upper()}\nBet: ${dollars}\n"
            f"Market: {open_ticker}\nBalance: ${balance:.2f}\nETH YES: {eth_yes_cents}c\n",
        )
    elif placed is False:
        send_email(
            f"[Kalshi] FAILED {series} {signal} {side.upper()}",
            f"Order rejected — see GitHub Actions logs.\n\n"
            f"Signal: {signal}\nBet: ${dollars}\nMarket: {open_ticker}\n",
        )

    if placed is True:
        series_state.update({
            "last_bet_event":    open_event,
            "last_bet_at":       now_utc.isoformat(timespec="seconds"),
            "last_bet_signal":   signal,
            "last_bet_side":     side,
            "last_bet_dollars":  dollars,
            "last_bet_ticker":   open_ticker,
            "last_bet_balance":  round(balance, 2),
            "last_bet_reported": False,
        })
        state["stats"]["bets"] += 1
        save_state(state)


# ── outcome tracking ───────────────────────────────────────────────────────────

def check_outcomes(state, balance):
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.setdefault("daily", {"date": today, "pnl": 0.0})
    if daily.get("date") != today:
        daily["date"] = today
        daily["pnl"]  = 0.0

    for series, ss in state.get("series", {}).items():
        ticker   = ss.get("last_bet_ticker", "")
        side     = ss.get("last_bet_side", "")
        dollars  = ss.get("last_bet_dollars", 0)
        signal   = ss.get("last_bet_signal", "")
        reported = ss.get("last_bet_reported", False)
        if not ticker or reported:
            continue

        code, resp = kalshi_get(f"/markets/{ticker}")
        if code != 200:
            continue
        mkt    = resp.get("market", resp)
        status = mkt.get("status", "")
        result = mkt.get("result", "")
        if status not in ("settled", "finalized") or result not in ("yes", "no"):
            continue

        won = (result == side)
        ss["last_bet_reported"] = True
        fee = dollars * 0.07 * 0.50
        pnl = round(dollars - fee, 2) if won else -dollars
        daily["pnl"] = round(daily.get("pnl", 0.0) + pnl, 2)
        save_state(state)

        log(f"  [{series}] {ticker} {result.upper()} -> {'WIN' if won else 'loss'}  pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}")

        if won:
            send_email(
                f"[Kalshi] WIN +${pnl:.2f} — {series} {signal}",
                f"Series: {series}\nSignal: {signal}\nBet: {side.upper()} ${dollars}\n"
                f"Result: {result.upper()} -> WIN\nProfit: +${pnl:.2f}\n"
                f"Market: {ticker}\nBalance: ${balance:.2f}\n",
            )


# ── main ──────────────────────────────────────────────────────────────────────

def run_once(dry_run=False):
    state   = load_state()
    now_utc = datetime.now(timezone.utc)
    log(f"=== CRYPTO15M @ {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")

    balance = fetch_balance()
    if balance is None:
        log("  WARNING: could not fetch balance — using fallback $500")
        balance = 500.0
    log(f"  balance=${balance:.2f}")

    check_outcomes(state, balance)

    # Fetch ETH anchor price once; shared across all follower XARB checks
    eth_yes_cents = None
    eth_open = fetch_open("KXETH15M")
    if eth_open:
        eth_yes_cents, _ = fetch_market_price(eth_open.get("ticker", ""))
    log(f"  ETH anchor YES={eth_yes_cents}c")

    for series, config in SERIES_CONFIG.items():
        poll_series(series, config, state, now_utc, dry_run, balance, eth_yes_cents)

    log("=== done ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",    action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status",  action="store_true")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(load_state(), indent=2))
        return
    try:
        run_once(dry_run=a.dry_run)
    except Exception as e:
        log(f"FATAL: {e}")
        raise


if __name__ == "__main__":
    main()
