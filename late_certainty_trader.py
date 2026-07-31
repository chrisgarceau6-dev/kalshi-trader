#!/usr/bin/env python3
"""Late-certainty trader — collect the premium on near-decided 15-min crypto markets.

STRATEGY:
  When any open 15-min crypto market has YES or NO ask in [88, 93] cents AND
  the window has 60-900 seconds remaining, buy that side.  Hold to settlement.

WHY IT WORKS (from empirical data over 500 markets, 40,000+ trades):
  * YES @ 85-89c in final 5 min: 100% resolved YES  (n=969)
  * YES @ 90-94c in final 5 min: 100% resolved YES  (n=3,085)
  * NO  @ 85-89c in final 5 min: 100% resolved NO   (n=703)  *not final 60s
  * NO  @ 90-94c in final 5 min: 100% resolved NO   (n=2,905)
  Wilson 95% CI on true win rate: >=99.6%

  By the time price reaches 88c+ with 1+ min left, the underlying is already
  well past the strike (or moving strongly toward it).  The remaining 5-15c
  is insurance premium retail buyers pay for near-certainty.  We collect it.

ECONOMICS (per $5 bet at 89c limit, 5 contracts):
  Win  (~99%+):  +$0.51  (10% return)
  Loss (~1%):    -$4.45
  EV per trade: +$0.46

usage: --once | --dry-run | --status
"""

import argparse, base64, json, os, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from kalshi_auth import get as _get, place_order

BASE       = Path(__file__).parent
STATE_FILE = BASE / "certainty_state.json"
LOG_FILE   = BASE / "certainty.log"

# ── strategy constants ─────────────────────────────────────────────────────────
SERIES_LIST     = ["KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]

MIN_ASK_CENTS   = 88     # min price we'll buy at (fatter margin below → riskier WR)
MAX_ASK_CENTS   = 93     # max price (>93 profit too thin vs fees)
LIMIT_BUFFER    = 1      # bid limit at ask + this many cents (slippage safety)

MIN_SECS_LEFT   = 60     # skip final <60s (per-data, NO side has 84% WR there)
MAX_SECS_LEFT   = 900    # skip if too early (unlikely to see 88c ask yet)

BET_DOLLARS     = 5      # conservative start

# Kill switches
STOP_BALANCE       = 300      # halt if balance drops below this
DAILY_LOSS_LIMIT   = 25       # halt for the day if -$25+ today
CONSEC_LOSS_LIMIT  = 3        # halt for 60 min after 3 consecutive losses


# ── infrastructure (reused from crypto15m_trader pattern) ─────────────────────

def _ensure_key():
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        return
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return
    p   = Path("/tmp/kalshi_certainty_key.pem")
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
    return {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}


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
        return None
    try:
        return float(resp.get("balance_dollars", 0))
    except Exception:
        return None


def send_email(subject, body):
    to_addr   = os.environ.get("COPY_EMAIL_TO", "")
    from_addr = os.environ.get("COPY_EMAIL_FROM", "")
    password  = os.environ.get("COPY_EMAIL_PASSWORD", "")
    if not (to_addr and from_addr and password):
        return
    try:
        msg = MIMEText(body, "plain")
        msg["From"], msg["To"], msg["Subject"] = from_addr, to_addr, subject
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(from_addr, password)
            s.send_message(msg)
        log(f"  email sent: {subject}")
    except Exception as e:
        log(f"  email failed: {e}")


# ── market scanning ────────────────────────────────────────────────────────────

def open_markets_near_close(series):
    """Return open markets for series with 60-900s remaining."""
    code, r = kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 10})
    if code != 200:
        return []
    now = datetime.now(timezone.utc)
    out = []
    for m in r.get("markets", []):
        ct = m.get("close_time", "")
        if not ct:
            continue
        try:
            close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            secs = (close_dt - now).total_seconds()
        except Exception:
            continue
        if MIN_SECS_LEFT <= secs <= MAX_SECS_LEFT:
            m["_secs_left"] = secs
            out.append(m)
    return out


# ── kill switches ──────────────────────────────────────────────────────────────

def check_halts(state, balance):
    """Returns (halt: bool, reason: str)."""
    if balance is not None and balance <= STOP_BALANCE:
        return True, f"balance ${balance:.2f} <= stop ${STOP_BALANCE}"
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.get("daily", {})
    if daily.get("date") == today and daily.get("pnl", 0) <= -DAILY_LOSS_LIMIT:
        return True, f"today's P&L ${daily['pnl']:+.2f} <= -${DAILY_LOSS_LIMIT}"
    # 60-min cooldown after N consecutive losses
    cl  = state.get("consec_losses", 0)
    ts  = state.get("last_loss_ts", 0)
    if cl >= CONSEC_LOSS_LIMIT:
        age = datetime.now(timezone.utc).timestamp() - ts
        if age < 3600:
            return True, f"{cl} consec losses, cooldown {60 - int(age/60)}min"
    return False, ""


# ── order placement ───────────────────────────────────────────────────────────

def try_trade(market, state, dry_run):
    ticker = market.get("ticker", "")
    if ticker in state.get("positions", {}):
        return  # already entered this market
    secs_left = market.get("_secs_left", 0)
    yes_ask   = int(round(float(market.get("yes_ask_dollars", 0) or 0) * 100))
    no_ask    = int(round(float(market.get("no_ask_dollars",  0) or 0) * 100))

    # Pick cheapest side within target band
    side, ask_cents = None, None
    if MIN_ASK_CENTS <= yes_ask <= MAX_ASK_CENTS:
        side, ask_cents = "yes", yes_ask
    elif MIN_ASK_CENTS <= no_ask <= MAX_ASK_CENTS:
        side, ask_cents = "no",  no_ask
    if side is None:
        return

    limit_cents = min(97, ask_cents + LIMIT_BUFFER)
    contracts   = max(1, int(BET_DOLLARS * 100 / limit_cents) + 1)  # ceil to spend ~$5
    est_cost    = contracts * limit_cents / 100
    est_profit  = contracts * (100 - limit_cents) / 100 * (1 - 0.07)  # after 7% fee on profit

    log(f"  TRADE: {ticker}  {secs_left:.0f}s left  {side.upper()} ask={ask_cents}c "
        f"limit={limit_cents}c  {contracts} contracts  cost=${est_cost:.2f}  est.win=+${est_profit:.2f}")

    if dry_run:
        log(f"    [dry-run] skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log(f"    KALSHI_API_KEY_ID not set — cannot trade")
        return

    code, resp = place_order(
        ticker, side, contracts,
        yes_price_cents=limit_cents if side == "yes" else None,
        no_price_cents=limit_cents  if side == "no"  else None,
    )
    if code in (200, 201):
        log(f"    order accepted")
        state["positions"][ticker] = {
            "side":        side,
            "limit_cents": limit_cents,
            "contracts":   contracts,
            "cost":        est_cost,
            "opened_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "settled":     False,
        }
        state["stats"]["trades"] += 1
        save_state(state)
        send_email(
            f"[Kalshi-C] Trade {ticker} {side.upper()} @ {limit_cents}c",
            f"Bought {contracts} {side.upper()} contracts @ {limit_cents}c on {ticker}\n"
            f"Cost: ${est_cost:.2f}\n"
            f"Expected win: +${est_profit:.2f}\n"
            f"Seconds left: {secs_left:.0f}\n",
        )
    else:
        log(f"    order FAILED — HTTP {code}: {str(resp)[:200]}")


# ── outcome tracking ───────────────────────────────────────────────────────────

def check_outcomes(state, balance):
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.setdefault("daily", {"date": today, "pnl": 0.0})
    if daily.get("date") != today:
        daily["date"] = today
        daily["pnl"]  = 0.0

    for ticker, pos in list(state.get("positions", {}).items()):
        if pos.get("settled"):
            continue
        code, resp = kalshi_get(f"/markets/{ticker}")
        if code != 200:
            continue
        m = resp.get("market", resp)
        status = m.get("status", "")
        result = m.get("result", "")
        if status not in ("settled", "finalized") or result not in ("yes", "no"):
            continue

        won  = (result == pos["side"])
        payout = pos["contracts"] if won else 0
        fee_on_profit = 0.07 * max(0, payout - pos["cost"])
        pnl = round(payout - pos["cost"] - fee_on_profit, 2)

        pos["settled"] = True
        pos["result"]  = result
        pos["pnl"]     = pnl

        daily["pnl"] = round(daily.get("pnl", 0.0) + pnl, 2)
        state["stats"]["pnl"] = round(state["stats"].get("pnl", 0.0) + pnl, 2)
        if won:
            state["stats"]["wins"] = state["stats"].get("wins", 0) + 1
            state["consec_losses"] = 0
        else:
            state["consec_losses"] = state.get("consec_losses", 0) + 1
            state["last_loss_ts"]  = datetime.now(timezone.utc).timestamp()

        save_state(state)
        wr = state["stats"]["wins"] / state["stats"]["trades"] if state["stats"]["trades"] else 0
        log(f"  SETTLED {ticker} result={result.upper()}  side={pos['side'].upper()}  "
            f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  cumul WR={wr*100:.1f}%")

        if won:
            send_email(
                f"[Kalshi-C] WIN +${pnl:.2f} — {ticker}",
                f"Bought {pos['contracts']} {pos['side'].upper()} @ {pos['limit_cents']}c\n"
                f"Result: {result.upper()} → WIN\n"
                f"Profit: +${pnl:.2f} (after 7% fee)\n"
                f"Balance: ${balance:.2f}\n"
                f"Cumulative: {state['stats']['wins']}/{state['stats']['trades']} = {wr*100:.1f}% WR\n",
            )
        else:
            send_email(
                f"[Kalshi-C] LOSS -${abs(pnl):.2f} — {ticker}",
                f"Bought {pos['contracts']} {pos['side'].upper()} @ {pos['limit_cents']}c\n"
                f"Result: {result.upper()} → LOSS\n"
                f"Loss: ${pnl:+.2f}\n"
                f"Consec losses: {state['consec_losses']}\n"
                f"Balance: ${balance:.2f}\n",
            )


# ── main ──────────────────────────────────────────────────────────────────────

def run_once(dry_run=False):
    state   = load_state()
    now_utc = datetime.now(timezone.utc)
    log(f"=== CERTAINTY @ {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC ===")

    balance = fetch_balance()
    if balance is None:
        log("  WARNING: cannot fetch balance — skipping this cycle")
        return
    log(f"  balance=${balance:.2f}")

    check_outcomes(state, balance)

    halted, reason = check_halts(state, balance)
    if halted:
        log(f"  HALTED — {reason}")
        return

    n_scanned, n_tradeable = 0, 0
    for series in SERIES_LIST:
        markets = open_markets_near_close(series)
        n_scanned += len(markets)
        for m in markets:
            before = len(state.get("positions", {}))
            try_trade(m, state, dry_run)
            if len(state.get("positions", {})) > before:
                n_tradeable += 1

    stats = state["stats"]
    wr = stats["wins"] / stats["trades"] if stats["trades"] else 0
    log(f"  scanned {n_scanned} near-close markets, new trades: {n_tradeable}")
    log(f"  cumulative: {stats['wins']}/{stats['trades']} = {wr*100:.1f}% WR  P&L=${stats['pnl']:+.2f}")
    log(f"=== done ===")


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
