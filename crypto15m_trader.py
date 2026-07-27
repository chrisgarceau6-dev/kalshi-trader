#!/usr/bin/env python3
"""Multi-asset 15-min Kalshi trader — M1/M2/M3 + behavioral signals.

usage: --once (live) | --dry-run (no orders) | --status (dump state)
"""

import argparse, base64, json, os, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from kalshi_auth import get as _get, place_order

BASE       = Path(__file__).parent
STATE_FILE = BASE / "crypto15m_state.json"
LOG_FILE   = BASE / "crypto15m.log"

# ── constants ─────────────────────────────────────────────────────────────────

KELLY_PCT = {
    "KXETH15M": 0.040,
    "KXSOL15M": 0.018,
    "KXBTC15M": 0.010,
}

MAG_MULTIPLIERS = [
    (0.010, 99.0, 1.4),
    (0.005, 0.010, 1.4),
    (0.003, 0.005, 1.0),
]

M2_MULTIPLIER   = 1.6
M3_MULTIPLIER   = 2.2
HOUR_MULTIPLIER = 1.0

# behavioral signal constants (69-day Kalshi validated, z>2.0 each)

BEHAVIORAL_KELLY = {
    "KXETH15M":  0.025,
    "KXSOL15M":  0.020,
    "KXDOGE15M": 0.020,
    "KXBNB15M":  0.018,
}

BEHAVIORAL_MULT = {
    "HD23DN": 1.4, "HD04UP": 1.4, "HD19DN": 1.3,
    "HD10DN": 1.2, "HD02UP": 1.3, "XDIV":   1.2,
    "SAT_ETH": 1.1, "SAT_DOGE": 1.0,
    "HB15": 1.0, "HB22": 1.0, "HB02": 1.0, "HB04": 1.0,
    "STRK_ETH": 1.1, "STRK_DOGE": 1.1, "STRK_SOL": 1.2, "STRK_BNB": 1.0,
}

MIN_BET         = 10
MAX_BET         = 500
MAX_PRICE_CENTS = 56
BIG_MOVE_PCT    = 0.003

SERIES_CONFIG = {
    "KXBTC15M":  {"apply_hour_signals": True,  "apply_behavioral": False},
    "KXETH15M":  {"apply_hour_signals": False,  "apply_behavioral": True},
    "KXSOL15M":  {"apply_hour_signals": False,  "apply_behavioral": True},
    "KXDOGE15M": {"apply_hour_signals": False,  "apply_behavioral": True},
    "KXBNB15M":  {"apply_hour_signals": False,  "apply_behavioral": True},
}


# ── auth / state / logging / balance / email ──────────────────────────────────

def _ensure_key():
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        return
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return
    p = Path("/tmp/kalshi_crypto15m_key.pem")
    b64  = raw.replace("\n", "").replace("\r", "").replace(" ", "")
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


# ── sizing / markets / signals / orders ───────────────────────────────────────

def _mag_mult(mag):
    for lo, hi, m in MAG_MULTIPLIERS:
        if lo <= mag < hi:
            return m
    return 1.0


def size_bet(series, signal, prev_mag, prev2_mag, balance):
    if signal[0] in ("M", "H"):
        kelly = KELLY_PCT.get(series, 0.02)
        if signal == "M3":
            dollars = balance * kelly * M3_MULTIPLIER * _mag_mult(max(prev_mag, prev2_mag))
        elif signal == "M2":
            dollars = balance * kelly * M2_MULTIPLIER * _mag_mult(max(prev_mag, prev2_mag))
        elif signal == "M1":
            dollars = balance * kelly * _mag_mult(prev_mag)
        else:
            dollars = balance * kelly * HOUR_MULTIPLIER
    else:
        kelly   = BEHAVIORAL_KELLY.get(series, 0.015)
        dollars = balance * kelly * BEHAVIORAL_MULT.get(signal, 1.0)

    return max(MIN_BET, min(MAX_BET, int(dollars)))

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


def fetch_settled(series, limit=6):
    mktlist = _markets_direct(series, "settled", limit) or _markets_via_events(series, "settled", limit)
    mktlist = [m for m in mktlist
               if m.get("result") in ("yes", "no")
               and m.get("floor_strike") and m.get("expiration_value")]
    return sorted(mktlist, key=lambda m: m.get("close_time", ""), reverse=True)


def fetch_open(series):
    mktlist = _markets_direct(series, "open", 3) or _markets_via_events(series, "open", 1)
    return mktlist[0] if mktlist else None

def evaluate_signal(hour_utc, prev_yes, prev2_yes, prev_mag, prev2_mag,
                    apply_hour_signals=True, prev3_yes=True, prev3_mag=0.0):
    if (not prev_yes) and (not prev2_yes) and (not prev3_yes) \
            and prev_mag > BIG_MOVE_PCT and prev2_mag > BIG_MOVE_PCT and prev3_mag > BIG_MOVE_PCT:
        return "M3", "yes"
    if (not prev_yes) and (not prev2_yes) and prev_mag > BIG_MOVE_PCT and prev2_mag > BIG_MOVE_PCT:
        return "M2", "yes"
    if (not prev_yes) and prev_mag > BIG_MOVE_PCT:
        return "M1", "yes"
    if not apply_hour_signals:
        return None, None
    if hour_utc == 13 and prev_yes:                return "H13", "no"
    if hour_utc == 0  and prev_yes:                return "H00", "no"
    if hour_utc == 1  and prev_yes and prev2_yes:  return "H01", "no"
    return None, None


def _res_yes(m):
    return m.get("result") == "yes"


def evaluate_behavioral(series, hour_utc, weekday, settled, cross_settled):
    if len(settled) < 2:
        return None, None

    prev_yes  = _res_yes(settled[0])
    prev2_yes = _res_yes(settled[1])
    is_sat    = (weekday == 5)

    # Cross-asset divergence: ETH↓ SOL↑ → SOL NO (z=+4.17, 58.5%)
    if series == "KXSOL15M":
        eth = cross_settled.get("KXETH15M", [])
        if eth and not _res_yes(eth[0]) and prev_yes:
            return "XDIV", "no"

    # Saturday patterns
    if series == "KXETH15M"  and is_sat and not prev_yes: return "SAT_ETH",  "yes"
    if series == "KXDOGE15M" and is_sat and prev_yes:     return "SAT_DOGE", "no"

    # Hour × direction (highest-confidence conditional signals)
    if series in ("KXETH15M", "KXBNB15M") and hour_utc == 19 and not prev_yes: return "HD19DN", "yes"
    if series == "KXETH15M"                and hour_utc == 2  and prev_yes:     return "HD02UP", "no"
    if series in ("KXSOL15M", "KXDOGE15M") and hour_utc == 23 and not prev_yes: return "HD23DN", "yes"
    if series == "KXDOGE15M"               and hour_utc == 4  and prev_yes:     return "HD04UP", "no"
    if series == "KXDOGE15M"               and hour_utc == 10 and not prev_yes: return "HD10DN", "yes"

    # Hour bias (unconditional)
    if series == "KXETH15M"                 and hour_utc == 15: return "HB15", "yes"
    if series in ("KXETH15M", "KXDOGE15M")  and hour_utc == 22: return "HB22", "no"
    if series == "KXSOL15M"                 and hour_utc == 2:  return "HB02", "no"
    if series == "KXBNB15M"                 and hour_utc == 4:  return "HB04", "no"

    # Streak fade (2+×UP → NO, 4+×DN → YES for SOL)
    if series == "KXETH15M"  and prev_yes and prev2_yes: return "STRK_ETH",  "no"
    if series == "KXDOGE15M" and prev_yes and prev2_yes: return "STRK_DOGE", "no"
    if series == "KXBNB15M"  and prev_yes and prev2_yes: return "STRK_BNB",  "no"
    if series == "KXSOL15M"  and len(settled) >= 4:
        if all(not _res_yes(settled[i]) for i in range(4)):
            return "STRK_SOL", "yes"

    return None, None


# ── order placement ───────────────────────────────────────────────────────────

def place_bet(ticker, side, dollars):
    """Return True=placed, False=API rejection, None=skipped (price/liquidity)."""
    code, resp = kalshi_get(f"/markets/{ticker}")
    if code != 200:
        log(f"  cannot fetch market {ticker} (HTTP {code})")
        return None

    market    = resp.get("market", resp)
    ask_field = "yes_ask_dollars" if side == "yes" else "no_ask_dollars"
    raw_ask   = market.get(ask_field)

    if raw_ask is None:
        log(f"  skip — no {side} ask (no liquidity)")
        return None

    ask_cents = int(round(float(raw_ask) * 100))
    if ask_cents > MAX_PRICE_CENTS:
        log(f"  skip — {side} ask is {ask_cents}¢ > max {MAX_PRICE_CENTS}¢")
        return None

    limit_cents = min(MAX_PRICE_CENTS, ask_cents + 1)
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

def poll_series(series, config, state, now_utc, dry_run, balance, cross_settled, settled_cache):
    apply_hour = config.get("apply_hour_signals", False)
    apply_beh  = config.get("apply_behavioral",   False)
    series_state = state.setdefault("series", {}).setdefault(series, {"last_bet_event": ""})

    log(f"  [{series}]")

    settled = settled_cache.get(series) or fetch_settled(series, limit=6)
    if len(settled) < 2:
        log(f"    only {len(settled)} settled — skipping")
        return

    prev,  prev2 = settled[0], settled[1]
    prev3        = settled[2] if len(settled) >= 3 else None

    def mkt_stats(m):
        yes = m.get("result") == "yes"
        s, e = float(m["floor_strike"]), float(m["expiration_value"])
        return yes, (abs(e - s) / s if s > 0 else 0)

    prev_yes,  prev_mag  = mkt_stats(prev)
    prev2_yes, prev2_mag = mkt_stats(prev2)
    prev3_yes  = prev3.get("result") == "yes" if prev3 else True
    prev3_mag  = mkt_stats(prev3)[1]           if prev3 else 0.0

    log(f"    prev  {'UP' if prev_yes else 'DN'}  {prev_mag:.3%}  "
        f"prev2 {'UP' if prev2_yes else 'DN'}  {prev2_mag:.3%}  "
        f"prev3 {'UP' if prev3_yes else 'DN'}  {prev3_mag:.3%}")

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
        weekday  = open_dt.weekday()
    except Exception:
        log(f"    cannot parse open_time {open_time!r} — skipping")
        return

    try:
        close_dt  = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        secs_left = (close_dt - now_utc).total_seconds()
        if secs_left < 240:
            log(f"    only {secs_left:.0f}s left — skip")
            return
        log(f"    {secs_left:.0f}s left")
    except Exception:
        pass

    # M-signals take priority (5yr OOS validated)
    signal, side = evaluate_signal(
        hour_utc, prev_yes, prev2_yes, prev_mag, prev2_mag,
        apply_hour_signals=apply_hour,
        prev3_yes=prev3_yes, prev3_mag=prev3_mag,
    )

    # Fallback to behavioral signals
    if not signal and apply_beh:
        signal, side = evaluate_behavioral(series, hour_utc, weekday, settled, cross_settled)

    if not signal:
        log(f"    no signal (h={hour_utc} prev={'UP' if prev_yes else 'DN'} {prev_mag:.3%})")
        return

    dollars = size_bet(series, signal, prev_mag, prev2_mag, balance)
    log(f"    SIGNAL {signal} → BET {side.upper()} ${dollars}  (balance=${balance:.0f})")

    if dry_run:
        log(f"    [dry-run] order skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log(f"    KALSHI_API_KEY_ID not set — cannot trade")
        return

    placed = place_bet(open_ticker, side, dollars)

    if placed is True:
        subject = f"[Kalshi] ✅ Trade placed — {series} {signal} {side.upper()} ${dollars}"
        body = (
            f"Signal detected and order placed ✅.\n\n"
            f"Series:   {series}\n"
            f"Signal:   {signal}\n"
            f"Side:     {side.upper()}\n"
            f"Bet:      ${dollars}\n"
            f"Market:   {open_ticker}\n"
            f"Balance:  ${balance:.2f}\n\n"
            f"Check your Kalshi account to confirm the open position.\n"
        )
        send_email(subject, body)
    elif placed is False:
        subject = f"[Kalshi] ❌ Order FAILED — {series} {signal} {side.upper()} ${dollars}"
        body = (
            f"Signal detected but order FAILED ❌.\n\n"
            f"Series:   {series}\n"
            f"Signal:   {signal}\n"
            f"Side:     {side.upper()}\n"
            f"Bet:      ${dollars}\n"
            f"Market:   {open_ticker}\n"
            f"Balance:  ${balance:.2f}\n\n"
            f"Order rejected — check GitHub Actions logs for details.\n"
        )
        send_email(subject, body)
    # placed is None = silent skip (price too high, no liquidity) — no email

    if placed is True:
        series_state["last_bet_event"]    = open_event
        series_state["last_bet_at"]       = now_utc.isoformat(timespec="seconds")
        series_state["last_bet_signal"]   = signal
        series_state["last_bet_side"]     = side
        series_state["last_bet_dollars"]  = dollars
        series_state["last_bet_ticker"]   = open_ticker
        series_state["last_bet_balance"]  = round(balance, 2)
        series_state["last_bet_reported"] = False
        state["stats"]["bets"] += 1
        save_state(state)


def check_outcomes(state, balance):
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
        save_state(state)

        fee = dollars * 0.07 * 0.50
        pnl = round(dollars - fee, 2) if won else -dollars
        log(f"  [{series}] {ticker} settled {result.upper()} → {'WIN ✓' if won else 'loss ✗'}  pnl=${pnl:+.2f}")

        if won:
            subject = f"[Kalshi] WIN +${pnl:.2f} — {series} {signal}"
            body    = (
                f"Trade settled as a WIN.\n\n"
                f"Series:   {series}\n"
                f"Signal:   {signal}\n"
                f"Bet:      {side.upper()} ${dollars}\n"
                f"Result:   {result.upper()} → WIN\n"
                f"Profit:   +${pnl:.2f} (after fee)\n"
                f"Market:   {ticker}\n\n"
                f"Account balance: ${balance:.2f}\n"
            )
            send_email(subject, body)


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

    settled_cache = {}
    for series in SERIES_CONFIG:
        settled_cache[series] = fetch_settled(series, limit=6)

    for series, config in SERIES_CONFIG.items():
        poll_series(series, config, state, now_utc, dry_run, balance,
                    cross_settled=settled_cache, settled_cache=settled_cache)

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
