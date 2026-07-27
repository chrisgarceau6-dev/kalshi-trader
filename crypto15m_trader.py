#!/usr/bin/env python3
"""Multi-asset 15-min mean-reversion trader for Kalshi.

Trades KXBTC15M, KXETH15M, and KXSOL15M in parallel.

Bet sizing: Kelly-based compounding + magnitude multipliers.
  Base bet = KELLY_PCT × current_balance (fetched live from Kalshi API).
  Magnitude multiplier scales the bet up when the setup is stronger
  (larger drop = higher historical win rate = more confidence = bigger bet).

  Kelly fractions (half-Kelly for safety):
    ETH M1: OOS WR=55.0%, half-Kelly ≈ 3.3% of balance
    SOL M1: OOS WR=53.5%, half-Kelly ≈ 1.8% of balance
    BTC M1: uncertain — 1.0% of balance (quarter-Kelly, monitor live)

  Magnitude multipliers (backtested win rates from 5yr KuCoin data):
    DN 0.3–0.5%  → 1.0×  (ETH 55%, SOL 53%)
    DN 0.5–1.0%  → 1.4×  (ETH 60%, SOL 56%)
    DN >1.0%     → 1.0×  (few data points, no extra sizing)
    Double DN    → 1.6×  (ETH 57%, SOL 59% — consecutive signal)

  Hard caps:
    MIN_BET = $10   (Kalshi minimum meaningful order)
    MAX_BET = $500  (absolute ceiling per trade)

Signal priority (first match per series per period):
  M2: prev DN>0.3% AND prev2 DN>0.3%  → YES  ETH 57%, SOL 60%
  M1: prev DN>0.3%                     → YES  ETH 55%, SOL 54%
  H13: hour==13 AND prev UP            → NO   BTC 54% (OOS validated)
  H00: hour==0  AND prev UP            → NO   BTC 58% (OOS validated)
  H01: hour==1  AND prev UP AND prev2 UP → NO BTC 55% (OOS validated)

H-signals applied only to KXBTC15M where 5yr OOS validation exists.

Runs via GitHub Actions cron: 0,15,30,45 * * * *
State persisted in crypto15m_state.json via Actions cache.

usage:
    python crypto15m_trader.py --once       # live trade (default)
    python crypto15m_trader.py --dry-run    # detect signals, no orders
    python crypto15m_trader.py --status     # print current state JSON
"""

import argparse, base64, json, os, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from kalshi_auth import get as _get, place_order

BASE       = Path(__file__).parent
STATE_FILE = BASE / "crypto15m_state.json"
LOG_FILE   = BASE / "crypto15m.log"

# ── bet sizing constants ───────────────────────────────────────────────────────

# Half-Kelly fractions per series (fraction of balance for a base M1 bet)
KELLY_PCT = {
    "KXETH15M": 0.033,   # ETH OOS WR=55.0%, half-Kelly=3.3%
    "KXSOL15M": 0.018,   # SOL OOS WR=53.5%, half-Kelly=1.8%
    "KXBTC15M": 0.010,   # BTC uncertain — quarter-Kelly conservatively
}

# Magnitude multipliers: how much bigger to bet when the drop is larger
# Thresholds are fractions (0.005 = 0.5%)
MAG_MULTIPLIERS = [
    (0.010, 99.0, 1.0),   # DN >1.0%: no extra sizing (too few data points)
    (0.005, 0.010, 1.4),  # DN 0.5–1.0%: historically ~59-60% WR on ETH
    (0.003, 0.005, 1.0),  # DN 0.3–0.5%: baseline
]

M2_MULTIPLIER   = 1.6   # consecutive down moves: additional confidence
HOUR_MULTIPLIER = 1.0   # H-signals: no magnitude info, use flat sizing

MIN_BET = 10    # dollars — below this Kalshi orders are impractical
MAX_BET = 500   # dollars — hard ceiling per trade regardless of balance

# ── series config ─────────────────────────────────────────────────────────────

SERIES_CONFIG = {
    # BTC: 5yr KuCoin shows 48.9% (uncertain) — run at quarter-Kelly, hour signals on
    "KXBTC15M": {"apply_hour_signals": True},
    # ETH: 5yr OOS WR=55.0% z=10.36 — half-Kelly
    "KXETH15M": {"apply_hour_signals": False},
    # SOL: 5yr OOS WR=53.5% z=8.95  — half-Kelly
    "KXSOL15M": {"apply_hour_signals": False},
}

MAX_PRICE_CENTS = 56    # refuse if ask > 56¢
BIG_MOVE_PCT    = 0.003  # 0.3% — confirmed threshold on 5yr data


# ── auth ──────────────────────────────────────────────────────────────────────

def _ensure_key():
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        return
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return
    p = Path("/tmp/kalshi_crypto15m_key.pem")
    if raw.startswith("-----"):
        # Raw PEM stored directly in the secret (possibly with literal \n sequences)
        p.write_text(raw.replace("\\n", "\n") + "\n")
    else:
        # Base64-encoded PEM — strip whitespace, fix padding, decode
        b64 = raw.replace("\n", "").replace("\r", "").replace(" ", "")
        b64 += "=" * (-len(b64) % 4)
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
    return {"series": {}, "stats": {"bets": 0, "pnl_dollars": 0.0}}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str))


def log(msg):
    ts   = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


# ── balance fetching ──────────────────────────────────────────────────────────

def fetch_balance():
    """Return available balance in dollars, or None on failure."""
    code, resp = kalshi_get("/portfolio/balance")
    if code != 200:
        return None
    try:
        cents = resp.get("balance") or resp.get("available_balance_cents", 0)
        return float(cents) / 100
    except Exception:
        return None


# ── email notifications ───────────────────────────────────────────────────────

def send_email(subject, body):
    to_addr   = os.environ.get("COPY_EMAIL_TO", "")
    from_addr = os.environ.get("COPY_EMAIL_FROM", "")
    password  = os.environ.get("COPY_EMAIL_PASSWORD", "")
    if not (to_addr and from_addr and password):
        return
    try:
        msg = MIMEText(body, "plain")
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


# ── bet sizing ────────────────────────────────────────────────────────────────

def _mag_multiplier(mag):
    """Return the magnitude multiplier for a given move size."""
    for lo, hi, mult in MAG_MULTIPLIERS:
        if lo <= mag < hi:
            return mult
    return 1.0


def size_bet(series, signal, prev_mag, prev2_mag, balance):
    """
    Compute dollar bet size using Kelly fraction × balance × magnitude multiplier.
    Returns an integer dollar amount between MIN_BET and MAX_BET.
    """
    kelly = KELLY_PCT.get(series, 0.02)
    base  = balance * kelly

    if signal == "M2":
        # Both drops contribute to magnitude; use the larger one
        mag_mult = _mag_multiplier(max(prev_mag, prev2_mag))
        dollars  = base * M2_MULTIPLIER * mag_mult
    elif signal == "M1":
        dollars  = base * _mag_multiplier(prev_mag)
    else:
        # Hour signals (H00, H01, H13) — no magnitude info
        dollars  = base * HOUR_MULTIPLIER

    dollars = max(MIN_BET, min(MAX_BET, dollars))
    return int(dollars)


# ── market fetching ───────────────────────────────────────────────────────────

def _markets_direct(series, status, limit):
    code, data = kalshi_get("/markets", {"series_ticker": series, "status": status, "limit": limit})
    if code == 200:
        return data.get("markets", [])
    return []


def _markets_via_events(series, status, limit):
    code, data = kalshi_get("/events", {"series_ticker": series, "status": status, "limit": limit})
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


def fetch_settled(series, limit=5):
    mktlist = _markets_direct(series, "settled", limit) or _markets_via_events(series, "settled", limit)
    mktlist = [
        m for m in mktlist
        if m.get("result") in ("yes", "no")
        and m.get("floor_strike")
        and m.get("expiration_value")
    ]
    return sorted(mktlist, key=lambda m: m.get("close_time", ""), reverse=True)


def fetch_open(series):
    mktlist = _markets_direct(series, "open", 3) or _markets_via_events(series, "open", 1)
    return mktlist[0] if mktlist else None


# ── signal logic ──────────────────────────────────────────────────────────────

def evaluate_signal(hour_utc, prev_yes, prev2_yes, prev_mag, prev2_mag, apply_hour_signals=True):
    """Return (signal_name, side) or (None, None). Sizing is done separately."""
    # M2: consecutive big down moves → strong bounce
    if (not prev_yes) and (not prev2_yes) and prev_mag > BIG_MOVE_PCT and prev2_mag > BIG_MOVE_PCT:
        return "M2", "yes"

    # M1: single big down move → bounce
    if (not prev_yes) and prev_mag > BIG_MOVE_PCT:
        return "M1", "yes"

    if not apply_hour_signals:
        return None, None

    # H13: 1PM UTC up move reverts (BTC only, 5yr OOS validated)
    if hour_utc == 13 and prev_yes:
        return "H13", "no"

    # H00: midnight UTC up move reverts (BTC only)
    if hour_utc == 0 and prev_yes:
        return "H00", "no"

    # H01: two consecutive up moves at 1AM UTC revert (BTC only)
    if hour_utc == 1 and prev_yes and prev2_yes:
        return "H01", "no"

    return None, None


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


# ── per-series poll ───────────────────────────────────────────────────────────

def poll_series(series, config, state, now_utc, dry_run, balance):
    apply_hour   = config.get("apply_hour_signals", False)
    series_state = state.setdefault("series", {}).setdefault(series, {"last_bet_event": ""})

    log(f"  [{series}]")

    # 1. Pull last 2 settled periods
    settled = fetch_settled(series, limit=5)
    if len(settled) < 2:
        log(f"    only {len(settled)} settled — skipping")
        return

    prev, prev2 = settled[0], settled[1]
    prev_yes    = (prev.get("result") == "yes")
    prev2_yes   = (prev2.get("result") == "yes")
    prev_start  = float(prev["floor_strike"])
    prev_end    = float(prev["expiration_value"])
    prev2_start = float(prev2["floor_strike"])
    prev2_end   = float(prev2["expiration_value"])
    prev_mag    = abs(prev_end - prev_start)   / prev_start   if prev_start  > 0 else 0
    prev2_mag   = abs(prev2_end - prev2_start) / prev2_start if prev2_start > 0 else 0

    log(f"    prev  {'UP' if prev_yes else 'DN'}  {prev_mag:.3%}  "
        f"prev2 {'UP' if prev2_yes else 'DN'}  {prev2_mag:.3%}")

    # 2. Find open market
    open_mkt = fetch_open(series)
    if not open_mkt:
        log(f"    no open market — skipping")
        return

    open_ticker = open_mkt.get("ticker", "")
    open_event  = open_mkt.get("event_ticker", "")
    open_time   = open_mkt.get("open_time", "")

    if series_state.get("last_bet_event") == open_event:
        log(f"    already traded {open_event} — skip")
        return

    try:
        open_dt  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        hour_utc = open_dt.hour
    except Exception:
        log(f"    cannot parse open_time {open_time!r} — skipping")
        return

    # 3. Evaluate signal
    signal, side = evaluate_signal(
        hour_utc, prev_yes, prev2_yes, prev_mag, prev2_mag,
        apply_hour_signals=apply_hour
    )

    if not signal:
        log(f"    no signal (hour={hour_utc} prev={'UP' if prev_yes else 'DN'} {prev_mag:.3%})")
        return

    # 4. Size the bet
    dollars = size_bet(series, signal, prev_mag, prev2_mag, balance)
    kelly   = KELLY_PCT.get(series, 0.02)
    mag_m   = _mag_multiplier(max(prev_mag, prev2_mag) if signal == "M2" else prev_mag)
    extra_m = M2_MULTIPLIER if signal == "M2" else HOUR_MULTIPLIER if signal.startswith("H") else 1.0

    log(f"    SIGNAL {signal} → BET {side.upper()} ${dollars}  "
        f"(balance=${balance:.0f} × {kelly:.1%} Kelly × {mag_m:.1f}mag × {extra_m:.1f}sig = ${balance*kelly*mag_m*extra_m:.0f}→capped ${dollars})")

    if dry_run:
        log(f"    [dry-run] order skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log(f"    KALSHI_API_KEY_ID not set — cannot trade")
        return

    # 5. Place and record
    placed = place_bet(open_ticker, side, dollars)
    if placed:
        series_state["last_bet_event"]   = open_event
        series_state["last_bet_at"]      = now_utc.isoformat(timespec="seconds")
        series_state["last_bet_signal"]  = signal
        series_state["last_bet_side"]    = side
        series_state["last_bet_dollars"] = dollars
        series_state["last_bet_ticker"]  = open_ticker
        series_state["last_bet_kelly"]   = f"{kelly:.1%}"
        series_state["last_bet_balance"] = round(balance, 2)
        state["stats"]["bets"] += 1
        save_state(state)


# ── outcome tracking ─────────────────────────────────────────────────────────

def check_outcomes(state, balance):
    """Check if any previously placed bets have settled, email on wins."""
    series_state = state.get("series", {})
    for series, ss in series_state.items():
        ticker   = ss.get("last_bet_ticker", "")
        side     = ss.get("last_bet_side", "")
        dollars  = ss.get("last_bet_dollars", 0)
        signal   = ss.get("last_bet_signal", "")
        reported = ss.get("last_bet_reported", False)

        if not ticker or reported:
            continue

        # Fetch that specific market to see if it settled
        code, resp = kalshi_get(f"/markets/{ticker}")
        if code != 200:
            continue
        mkt    = resp.get("market", resp)
        status = mkt.get("status", "")
        result = mkt.get("result", "")

        if status not in ("settled", "finalized") or result not in ("yes", "no"):
            continue  # still open

        won = (result == side)
        ss["last_bet_reported"] = True
        save_state(state)

        outcome_str = "WIN ✓" if won else "loss ✗"
        fee   = dollars * 0.07 * 0.50
        pnl   = round(dollars - fee, 2) if won else -dollars
        log(f"  [{series}] {ticker} settled {result.upper()} → {outcome_str}  pnl=${pnl:+.2f}")

        if won:
            subject = f"[Kalshi] WIN +${pnl:.2f} — {series} {signal}"
            body = (
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


# ── main ──────────────────────────────────────────────────────────────────────

def run_once(dry_run=False):
    state   = load_state()
    now_utc = datetime.now(timezone.utc)
    log(f"=== CRYPTO15M @ {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")

    # Fetch balance once and share across all series
    balance = fetch_balance()
    if balance is None:
        log("  WARNING: could not fetch balance — using fallback $500")
        balance = 500.0
    log(f"  balance=${balance:.2f}  (bets will scale with this)")

    # Check if any previous bets have settled; email on wins
    check_outcomes(state, balance)

    for series, config in SERIES_CONFIG.items():
        poll_series(series, config, state, now_utc, dry_run, balance)

    log("=== done ===")


def main():
    ap = argparse.ArgumentParser(description="Multi-asset 15-min mean-reversion trader")
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
