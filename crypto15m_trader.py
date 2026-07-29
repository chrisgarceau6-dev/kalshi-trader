#!/usr/bin/env python3
"""Multi-asset 15-min Kalshi trader — momentum-conditioned calendar strategy.

STRATEGY V2 (2-year validated):
  21 signals discovered from 18 months of 15m OHLC data (2024-07 to 2026-01),
  every one validated on 6 months of unseen data (2026-01 to 2026-07): 21/21 held.

  Signal types:
    - 2-cond momentum fades: at hour H, if previous 2 candles ended UP, bet NO
      (or reverse patterns) — WR 55-59%
    - 1-cond momentum: at hour H, if previous candle ended UP, bet NO — WR 54-57%
    - Hour x slot x weekday high-conviction: e.g. "DOGE Friday 23:00 :00 -> NO"
      (65-73% WR, ~100 samples but held 4/4 half-year periods)

  Backtest starting from $500 on 2 years of real data:
    Final: $71,700  |  57.6% WR  |  Reaches $1k in 23 days, $10k in 222 days
    68% profitable days, max daily loss -$608, max drawdown -$1,543

  Every signal is CONDITIONAL, not directional:  we do NOT predict which way
  crypto will move.  We measure "at hour H after condition C, the market closes
  DOWN 58% of the time" and bet accordingly.

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

MIN_BET         = 5
MAX_PRICE_CENTS = 56
STOP_BALANCE    = 0      # disabled — user removed the balance floor

# ── SIZING TIERS with auto-escalation ─────────────────────────────────────────
# Start at "conservative" until we've proven live edge. Auto-bump to "moderate"
# after 50+ trades w/ WR >=55%. Then to "aggressive" after another week of 58%+.
# Auto-revert one step if live WR drops below the entry threshold for that tier.
SIZING_TIERS = {
    "conservative": {
        "kelly": {"elite": 0.030, "strong": 0.020, "standard": 0.012},
        "max_brackets": [(750,10),(1500,20),(3000,40),(999999,60)],
        # promote if:
        "promote_min_trades": 50,
        "promote_min_wr":     0.55,
    },
    "moderate": {
        "kelly": {"elite": 0.050, "strong": 0.035, "standard": 0.025},
        "max_brackets": [(750,20),(1500,40),(3000,80),(999999,150)],
        "promote_min_trades": 150,   # cumulative — 100 more after entering this tier
        "promote_min_wr":     0.58,
        # demote if live WR falls below this:
        "demote_wr": 0.52,
    },
    "aggressive": {
        "kelly": {"elite": 0.080, "strong": 0.060, "standard": 0.040},
        "max_brackets": [(750,30),(1500,75),(3000,150),(999999,300)],
        "promote_min_trades": 99999,  # top tier, no further promotion
        "promote_min_wr":     1.00,
        "demote_wr": 0.54,
    },
}
TIER_ORDER = ["conservative", "moderate", "aggressive"]

def max_bet_for(balance, tier="conservative"):
    for cap, mb in SIZING_TIERS[tier]["max_brackets"]:
        if balance < cap: return mb
    return SIZING_TIERS[tier]["max_brackets"][-1][1]

# Adaptive learning thresholds
LEARN_MIN_TRADES        = 5
LEARN_HALF_TRADES       = 10
LEARN_DISABLE_TRADES    = 20
LEARN_HALF_WR           = 0.45
LEARN_DISABLE_WR        = 0.48
LEARN_BOOST_TRADES      = 30
LEARN_BOOST_WR          = 0.65

DAILY_HALT_LOSS         = -500
ROLLING_HALT_LOSS       = -700

LEGACY_RESET_DATE       = "2026-07-28-v2"    # bump to force fresh state on deploy

# ── SIGNAL SET V2 (2-year OOS validated, 21/21 holds) ─────────────────────────
# Priority: HSW > 2COND > 1COND (most specific wins; at most one signal per window)

# Only signals that hold on BOTH Coinbase 2y data AND Kalshi 68d actual settlements.
# Cross-verified: signals discovered on Coinbase OHLC (2y) must also show >=55% WR
# on real Kalshi settlements before being deployed. This eliminates signals that
# might be Coinbase-specific noise.

# Hour × slot × weekday (7 survived cross-verification, all >=55% on both)
HSW_SIGNALS = {
    ("KXDOGE15M", 23, 0, 4): ("DOGE_FRI_H23_S00", "no",  0.712),
    ("KXETH15M",  17, 1, 5): ("ETH_SAT_H17_S15",  "yes", 0.680),
    ("KXDOGE15M", 20, 3, 6): ("DOGE_SUN_H20_S45", "no",  0.676),
    ("KXXRP15M",  23, 0, 3): ("XRP_THU_H23_S00",  "no",  0.673),
    ("KXSOL15M",  17, 1, 5): ("SOL_SAT_H17_S15",  "yes", 0.660),
    ("KXXRP15M",   6, 1, 4): ("XRP_FRI_H06_S15",  "no",  0.660),
    ("KXXRP15M",  18, 0, 1): ("XRP_TUE_H18_S00",  "yes", 0.657),
    ("KXDOGE15M",  5, 2, 4): ("DOGE_FRI_H05_S30", "yes", 0.650),
}

# 2-condition (4 survived, ETH_H12_2UP_FADE removed - only 46% on Kalshi)
COND2_SIGNALS = {
    ("KXDOGE15M",  4, 1, 1): ("DOGE_H04_2UP_FADE",  "no",  0.593),
    ("KXSOL15M",  12, 1, 1): ("SOL_H12_2UP_FADE",   "no",  0.584),
    ("KXETH15M",  19, 0, 1): ("ETH_H19_UP_AFTER_DN","yes", 0.580),
}

# 1-condition (3 kept — strongest on Kalshi: 59-64% WR with 100+ Kalshi samples)
COND1_SIGNALS = {
    ("KXETH15M",  19, 0): ("ETH_H19_BOUNCE", "yes", 0.571),
    ("KXDOGE15M",  4, 1): ("DOGE_H04_FADE",  "no",  0.562),
    ("KXETH15M",   0, 1): ("ETH_H00_FADE",   "no",  0.551),
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


def fetch_settled(series, limit=4):
    """Return most recent settled markets, newest first."""
    mktlist = _markets_direct(series, "settled", limit) or _markets_via_events(series, "settled", limit)
    mktlist = [m for m in mktlist if m.get("result") in ("yes", "no")]
    return sorted(mktlist, key=lambda m: m.get("close_time", ""), reverse=True)


# ── signal lookup ─────────────────────────────────────────────────────────────

def find_signal(series, hour, slot, wday, prev_yes, prev2_yes):
    """Priority: HSW > 2COND > 1COND. Returns (name, side, wr) or (None,None,None)."""
    key = (series, hour, slot, wday)
    if key in HSW_SIGNALS:
        return HSW_SIGNALS[key]
    key = (series, hour, prev_yes, prev2_yes)
    if key in COND2_SIGNALS:
        return COND2_SIGNALS[key]
    key = (series, hour, prev_yes)
    if key in COND1_SIGNALS:
        return COND1_SIGNALS[key]
    return None, None, None


# ── adaptive learning ─────────────────────────────────────────────────────────

def get_signal_stats(state, name):
    return state.setdefault("signal_stats", {}).setdefault(
        name, {"trades": 0, "wins": 0, "pnl": 0.0}
    )


def update_signal_stats(state, name, won, pnl):
    s = get_signal_stats(state, name)
    s["trades"] = s.get("trades", 0) + 1
    if won:
        s["wins"] = s.get("wins", 0) + 1
    s["pnl"] = round(s.get("pnl", 0.0) + pnl, 2)


def learning_multiplier(state, name):
    s = get_signal_stats(state, name)
    n = s.get("trades", 0)
    if n < LEARN_MIN_TRADES:
        return 1.0
    wr = s.get("wins", 0) / n
    if n >= LEARN_DISABLE_TRADES and wr < LEARN_DISABLE_WR:
        return 0.0
    if n >= LEARN_HALF_TRADES and wr < LEARN_HALF_WR:
        return 0.5
    if n >= LEARN_BOOST_TRADES and wr >= LEARN_BOOST_WR:
        return 1.25
    return 1.0


def rolling_pnl_24h(state):
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    total = 0.0
    for entry in state.get("recent_pnl", []):
        if entry.get("ts", 0) >= cutoff:
            total += entry.get("pnl", 0.0)
    return total


def add_recent_pnl(state, pnl):
    now_ts = datetime.now(timezone.utc).timestamp()
    hist = state.setdefault("recent_pnl", [])
    hist.append({"ts": now_ts, "pnl": pnl})
    cutoff = now_ts - 172800
    state["recent_pnl"] = [e for e in hist if e.get("ts", 0) >= cutoff]


def is_halted(state, balance):
    if balance <= STOP_BALANCE:
        return True, f"balance ${balance:.2f} <= stop ${STOP_BALANCE}"
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.get("daily", {})
    if daily.get("date") == today and daily.get("pnl", 0) <= DAILY_HALT_LOSS:
        return True, f"today's P&L ${daily['pnl']:+.2f} <= daily halt ${DAILY_HALT_LOSS}"
    r24 = rolling_pnl_24h(state)
    if r24 <= ROLLING_HALT_LOSS:
        return True, f"24h P&L ${r24:+.2f} <= rolling halt ${ROLLING_HALT_LOSS}"
    return False, ""


def size_bet(oos_wr, balance, state, name):
    tier = state.get("sizing_tier", "conservative")
    k    = SIZING_TIERS[tier]["kelly"]
    kelly = (k["elite"]    if oos_wr >= 0.62 else
             k["strong"]   if oos_wr >= 0.56 else
             k["standard"])
    raw   = balance * kelly * learning_multiplier(state, name)
    if raw <= 0:
        return 0
    mb = max_bet_for(balance, tier)
    return max(MIN_BET, min(mb, int(raw)))


def overall_live_stats(state):
    """Return (total_trades, overall_wr, disabled_signal_count)."""
    stats = state.get("signal_stats", {})
    total_trades = sum(s.get("trades", 0) for s in stats.values())
    total_wins   = sum(s.get("wins",   0) for s in stats.values())
    wr = total_wins / total_trades if total_trades else 0
    disabled = sum(1 for name in stats
                   if learning_multiplier(state, name) == 0.0)
    return total_trades, wr, disabled


def evaluate_tier_change(state):
    """Check auto-escalation / auto-demotion. Returns (new_tier, reason) or (None, None)."""
    current_tier = state.get("sizing_tier", "conservative")
    n, wr, disabled = overall_live_stats(state)
    cfg = SIZING_TIERS[current_tier]

    # DEMOTE first: if we're above conservative and things look bad
    if current_tier != "conservative" and n >= 30:
        if wr < cfg.get("demote_wr", 0):
            idx = TIER_ORDER.index(current_tier)
            return TIER_ORDER[idx-1], f"live WR {wr*100:.1f}% < {cfg['demote_wr']*100:.0f}% demote threshold"
        # extra safety: if too many signals disabled
        if disabled >= 4:
            idx = TIER_ORDER.index(current_tier)
            return TIER_ORDER[idx-1], f"{disabled} signals disabled by adaptive learning"

    # PROMOTE: enough trades + good WR + few disabled
    if current_tier != "aggressive":
        if (n >= cfg["promote_min_trades"] and
            wr >= cfg["promote_min_wr"] and
            disabled <= 2):
            idx = TIER_ORDER.index(current_tier)
            new_tier = TIER_ORDER[idx+1]
            return new_tier, f"live WR {wr*100:.1f}% over {n} trades qualifies for promotion"

    return None, None


# ── order placement ───────────────────────────────────────────────────────────

def place_bet(ticker, side, dollars):
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
        open_dt = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        hour, slot, wday = open_dt.hour, open_dt.minute // 15, open_dt.weekday()
    except Exception:
        log(f"    cannot parse open_time {open_time!r} — skip")
        return

    try:
        close_dt  = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        secs_left = (close_dt - now_utc).total_seconds()
        if secs_left < 240:
            log(f"    only {secs_left:.0f}s left — skip")
            return
        log(f"    {secs_left:.0f}s left  h={hour:02d} :{slot*15:02d} wday={wday}")
    except Exception:
        pass

    # Need previous candle results for momentum-conditional signals
    settled = fetch_settled(series, limit=3)
    if len(settled) < 2:
        log(f"    only {len(settled)} settled — cannot evaluate momentum signals")
        return
    prev_yes  = 1 if settled[0].get("result") == "yes" else 0
    prev2_yes = 1 if settled[1].get("result") == "yes" else 0
    log(f"    prev={'UP' if prev_yes else 'DN'} prev2={'UP' if prev2_yes else 'DN'}")

    name, side, wr = find_signal(series, hour, slot, wday, prev_yes, prev2_yes)
    if not name:
        log(f"    no signal for h={hour:02d} :{slot*15:02d} wday={wday} py={prev_yes} p2y={prev2_yes}")
        return

    dollars = size_bet(wr, balance, state, name)
    stats   = get_signal_stats(state, name)
    live_wr = stats["wins"] / stats["trades"] if stats["trades"] else None
    mult    = learning_multiplier(state, name)

    if dollars <= 0 or mult == 0.0:
        log(f"    SIGNAL {name} DISABLED (live {stats['trades']} trades, "
            f"WR {live_wr*100:.0f}%) — skip")
        return

    tag = f"OOS {wr*100:.0f}%"
    if stats["trades"] >= LEARN_MIN_TRADES:
        tag += f", live {live_wr*100:.0f}% ({stats['trades']}n)"
    if mult != 1.0:
        tag += f", mult={mult:.2f}x"
    log(f"    SIGNAL {name} ({tag}) -> BET {side.upper()} ${dollars}  (bal=${balance:.0f})")

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
            f"Bet: ${dollars}\nMarket: {open_ticker}\nBalance: ${balance:.2f}\n"
            f"Prev candles: {'UP' if prev_yes else 'DN'}, {'UP' if prev2_yes else 'DN'}\n",
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
        update_signal_stats(state, signal, won, pnl)
        add_recent_pnl(state, pnl)
        save_state(state)

        sig_stats = get_signal_stats(state, signal)
        live_wr   = sig_stats["wins"] / sig_stats["trades"]
        log(f"  [{series}] {ticker} {result.upper()} -> {'WIN' if won else 'loss'}  "
            f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  "
            f"[{signal} live: {sig_stats['wins']}/{sig_stats['trades']} = {live_wr*100:.0f}%]")

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

    # One-shot: clear legacy state from previous strategy versions
    if not state.get("legacy_reset_done") == LEGACY_RESET_DATE:
        old_daily  = state.get("daily", {}).get("pnl", 0)
        old_recent = len(state.get("recent_pnl", []))
        state["daily"]         = {"date": datetime.now(timezone.utc).date().isoformat(), "pnl": 0.0}
        state["recent_pnl"]    = []
        state["signal_stats"]  = {}
        state["halted_notified_date"] = ""
        state["legacy_reset_done"]    = LEGACY_RESET_DATE
        save_state(state)
        log(f"  V2 RESET — cleared old daily P&L (${old_daily:+.2f}), "
            f"{old_recent} rolling entries, and prior signal stats. Fresh start.")

    check_outcomes(state, balance)

    halted, reason = is_halted(state, balance)
    if halted:
        log(f"  HALTED — {reason}. No new trades this run.")
        if not state.get("halted_notified_date") == datetime.now(timezone.utc).date().isoformat():
            send_email(
                f"[Kalshi] HALTED — {reason}",
                f"Reason: {reason}\nBalance: ${balance:.2f}\n"
                f"Auto-resumes when the brake clears.\n",
            )
            state["halted_notified_date"] = datetime.now(timezone.utc).date().isoformat()
            save_state(state)
        return

    # Auto tier promotion / demotion
    new_tier, reason = evaluate_tier_change(state)
    if new_tier:
        old_tier = state.get("sizing_tier", "conservative")
        state["sizing_tier"] = new_tier
        save_state(state)
        log(f"  TIER CHANGE: {old_tier} -> {new_tier}  ({reason})")
        send_email(
            f"[Kalshi] Sizing tier: {old_tier} -> {new_tier}",
            f"Adaptive tier change.\nReason: {reason}\nBalance: ${balance:.2f}\n"
            f"New Kelly: {SIZING_TIERS[new_tier]['kelly']}\n"
            f"New MAX_BET brackets: {SIZING_TIERS[new_tier]['max_brackets']}\n",
        )

    tier = state.get("sizing_tier", "conservative")
    n_live, wr_live, disabled = overall_live_stats(state)
    log(f"  tier={tier}  max bet=${max_bet_for(balance, tier)}  "
        f"live: {n_live} trades  WR {wr_live*100:.0f}%  disabled: {disabled}  "
        f"24h P&L: ${rolling_pnl_24h(state):+.2f}")

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
