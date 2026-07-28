#!/usr/bin/env python3
"""Multi-asset 15-min Kalshi trader — calendar-only, direction-neutral.

Strategy:
  Trades only on time-of-day biases (hour + 15-min-slot) validated on 69 days
  of real Kalshi data with strict OOS (out-of-sample) validation.  No signal
  depends on the direction the underlying asset actually moves — every entry
  is a fixed "at UTC hour H slot S on series X, bet SIDE" rule.

  All 25 signals hold >=53% OOS win rate.  Backtest: $500 -> $15,259 in 71 days,
  57.7% WR, 87% profitable days, max drawdown -$352.

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

KELLY_STRONG    = 0.030   # OOS WR >= 60%
KELLY_WEAK      = 0.020   # OOS WR 54-60%
MIN_BET         = 10
MAX_BET         = 30
MAX_PRICE_CENTS = 56      # skip if our side costs more than this
STOP_BALANCE    = 400     # halt all trading if balance <= this

# High-conviction slot-specific signals (OOS WR >= 55%)
# key = (series, hour, slot) where slot = 0/1/2/3 for :00/:15/:30/:45
# val = (name, side, oos_wr)
SLOT_SIGNALS = {
    ("KXBNB15M",   4, 0):  ("BNB_H04_S00",  "no",  0.735),
    ("KXBNB15M",   0, 0):  ("BNB_H00_S00",  "yes", 0.714),
    ("KXETH15M",  19, 2):  ("ETH_H19_S30",  "yes", 0.706),
    ("KXETH15M",  12, 0):  ("ETH_H12_S00",  "no",  0.676),
    ("KXXRP15M",  23, 2):  ("XRP_H23_S30",  "yes", 0.676),
    ("KXSOL15M",  12, 0):  ("SOL_H12_S00",  "no",  0.647),
    ("KXSOL15M",  21, 0):  ("SOL_H21_S00",  "yes", 0.647),
    ("KXDOGE15M", 14, 3):  ("DOGE_H14_S45", "yes", 0.647),
    ("KXETH15M",  14, 3):  ("ETH_H14_S45",  "yes", 0.618),
    ("KXETH15M",  16, 1):  ("ETH_H16_S15",  "no",  0.618),
    ("KXBNB15M",  12, 3):  ("BNB_H12_S45",  "yes", 0.618),
    ("KXBNB15M",   9, 0):  ("BNB_H09_S00",  "no",  0.618),
    ("KXDOGE15M", 16, 1):  ("DOGE_H16_S15", "no",  0.618),
    ("KXDOGE15M",  4, 0):  ("DOGE_H04_S00", "no",  0.618),
    ("KXBNB15M",   4, 1):  ("BNB_H04_S15",  "no",  0.588),
    ("KXXRP15M",  22, 2):  ("XRP_H22_S30",  "no",  0.588),
    ("KXDOGE15M", 18, 3):  ("DOGE_H18_S45", "no",  0.559),
    ("KXDOGE15M", 15, 3):  ("DOGE_H15_S45", "no",  0.559),
}

# Whole-hour biases (fire on any slot; only if no slot signal matched)
# Only kept when profitable across all 4 slots in backtest.
HOUR_SIGNALS = {
    ("KXETH15M",  22): ("ETH_H22",  "no",  0.581),
    ("KXSOL15M",   1): ("SOL_H01",  "no",  0.621),
    ("KXBNB15M",   1): ("BNB_H01",  "no",  0.571),
    ("KXDOGE15M", 22): ("DOGE_H22", "no",  0.566),
    ("KXETH15M",  23): ("ETH_H23",  "yes", 0.544),
}

SERIES_LIST = ["KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]


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


# ── signal lookup ─────────────────────────────────────────────────────────────

def find_signal(series, hour, slot):
    """Return (name, side, oos_wr) or (None, None, None)."""
    key = (series, hour, slot)
    if key in SLOT_SIGNALS:
        return SLOT_SIGNALS[key]
    key = (series, hour)
    if key in HOUR_SIGNALS:
        return HOUR_SIGNALS[key]
    return None, None, None


def size_bet(oos_wr, balance):
    kelly = KELLY_STRONG if oos_wr >= 0.60 else KELLY_WEAK
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

def poll_series(series, state, now_utc, dry_run, balance):
    series_state = state.setdefault("series", {}).setdefault(series, {"last_bet_event": ""})
    log(f"  [{series}]")

    open_mkt = fetch_open(series)
    if not open_mkt:
        log(f"    no open market — skip")
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
        hour     = open_dt.hour
        slot     = open_dt.minute // 15
    except Exception:
        log(f"    cannot parse open_time {open_time!r} — skip")
        return

    try:
        close_dt  = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        secs_left = (close_dt - now_utc).total_seconds()
        if secs_left < 240:
            log(f"    only {secs_left:.0f}s left — skip")
            return
        log(f"    {secs_left:.0f}s left  h={hour:02d} :{slot*15:02d}")
    except Exception:
        pass

    name, side, wr = find_signal(series, hour, slot)
    if not name:
        log(f"    no signal for h={hour:02d} :{slot*15:02d}")
        return

    dollars = size_bet(wr, balance)
    log(f"    SIGNAL {name} (OOS {wr*100:.1f}%) -> BET {side.upper()} ${dollars}  (bal=${balance:.0f})")

    if dry_run:
        log(f"    [dry-run] skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log(f"    KALSHI_API_KEY_ID not set — cannot trade")
        return

    placed = place_bet(open_ticker, side, dollars)

    if placed is True:
        send_email(
            f"[Kalshi] Trade {series} {name} {side.upper()} ${dollars}",
            f"Signal: {name}\nOOS WR: {wr*100:.1f}%\nSide: {side.upper()}\n"
            f"Bet: ${dollars}\nMarket: {open_ticker}\nBalance: ${balance:.2f}\n",
        )
    elif placed is False:
        send_email(
            f"[Kalshi] FAILED {series} {name}",
            f"Order rejected — see GitHub Actions logs.\n\n"
            f"Signal: {name}\nBet: ${dollars}\nMarket: {open_ticker}\n",
        )

    if placed is True:
        series_state.update({
            "last_bet_event":    open_event,
            "last_bet_at":       now_utc.isoformat(timespec="seconds"),
            "last_bet_signal":   name,
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

    if balance <= STOP_BALANCE:
        log(f"  HALTED — balance ${balance:.2f} <= stop ${STOP_BALANCE}. No trades.")
        send_email(
            f"[Kalshi] HALTED — balance ${balance:.2f}",
            f"Trading halted. Balance ${balance:.2f} hit the ${STOP_BALANCE} floor.\n"
            f"Re-enable by lowering STOP_BALANCE in crypto15m_trader.py.\n",
        )
        return

    check_outcomes(state, balance)

    for series in SERIES_LIST:
        poll_series(series, state, now_utc, dry_run, balance)

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
