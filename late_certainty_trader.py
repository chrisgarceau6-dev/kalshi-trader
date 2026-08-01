#!/usr/bin/env python3
"""Late-certainty trader v5 — max-return filter from OOS-validated search.

STRATEGY:
  Buy YES or NO when its ask is in [90, 99] cents AND the window has
  150-900 seconds remaining AND all 3 preceding 1-min candles had ask
  (same side) >= 80 cents. Hold to settlement.

WHY v5 REPLACES v4:
  v4 was ranked #1 by WR. v5 is ranked #1 by TOTAL RETURN.
  User feedback: pursue max return, WR need not be 100%.

  Both filters pass OOS validation (train days 40-60 -> test days 0-20).
  v5 accepts slightly lower per-trade WR in exchange for ~4x volume and
  ~3x total profit.

BACKTEST (2026-08-01, 6 crypto series over 60 days):
                      v4 filter        v5 filter (this)
  60-day WR:          98.51%          95.96%
  60-day trades:      7,608            15,900
  60-day net@$50:     +$1,972         +$5,240
  20-day OOS WR:      98.72%           96.72% (edge STRENGTHENED)
  20-day OOS net:     +$821            +$3,574
  Per-series 60d:     98.3-98.6%       ~95-97% (all positive)

  Plus KXHYPE15M (added earlier):
    v5 filter:  60d n=2,655  WR=95.52%  net@$50=+$452

  Combined 7-series total: ~$5,692/60d @ $50 bets = ~$95/day expected.

ECONOMICS (avg entry ~93c):
  Win  (~96%):    +$0.13 per $2 bet, scales linearly to bet size
  Loss (~4%):     -$1.86 per $2 bet, scales linearly to bet size
  EV per trade:   +$0.05 per $2 bet

  Volume: ~265 trades/day across 7 crypto series (BTC/ETH/SOL/DOGE/BNB/XRP/HYPE).
  Expected losses: ~10-12/day (4% loss rate).

BET SIZING (auto-scales with balance):
  $500 balance -> $5 bets  (~$10/day expected)
  $700         -> $15
  $900         -> $25
  $1100        -> $35
  $1400+       -> $50 cap (~$95/day expected)

KILL SWITCHES:
  STOP_BALANCE=$350 (halt if balance drops here)
  DAILY_LOSS_LIMIT = 6x bet_size (dynamic — auto-scales)
  5 consecutive losses -> 60-min cooldown

usage: --once | --dry-run | --status

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
SERIES_LIST     = [
    # 15m crypto series where v4 filter (ask[95,99] prior_k=3 pmin=92 both)
    # showed +EV in 60-day backtest:
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M",
    "KXHYPE15M",  # added — 98.75% WR, +$442/60d @ $50 in v4 backtest
    # Explicitly EXCLUDED (v4 filter tested but was net-negative):
    # - KXNEAR15M (96.10% WR but small-cap tail risk lost money)
    # - KXZEC15M (95.03% WR, same issue)
    # - WTI/Gold/Silver 15m (insufficient historical data at backtest time)
]

MIN_ASK_CENTS   = 90     # v5: widened entry from [95,99] to [90,99] — more volume
MAX_ASK_CENTS   = 99
PRIOR_MIN_CENTS = 80     # v5: relaxed prior gate from 92c to 80c — catches more +EV entries
PRIOR_LOOKBACK  = 3      # 3 consecutive prior candles must have ask >= PRIOR_MIN_CENTS
YES_ONLY        = False  # both sides eligible
# TIGHT limit — small buffer above observed ask. Prevents catastrophic fills at
# way-below-ask prices (which happened in live trading when market crashed
# between scan and order-execution). If market moves >LIMIT_BUFFER cents up
# between scan and fill, we DON'T fill — we miss the trade, no harm.
LIMIT_BUFFER    = 2      # bid = ask + 2c (accepts tiny slippage, rejects worse)

# Increased from 60s -> 150s. Order placement takes 5-30s (network + Kalshi
# processing). If scan sees 61s remaining, order might land with <30s left,
# which puts us in the risky "final minute" bucket where NO has 84% WR (not 100).
MIN_SECS_LEFT   = 150    # ensure at least 2min left after order lands
MAX_SECS_LEFT   = 900

# ── ADAPTIVE BET SIZING ────────────────────────────────────────────────────
# Bet size auto-scales with account balance. Linear ramp from $5 (safe start)
# up to $50 (Kelly-informed cap for 96% WR strategy). Reads fresh balance
# every scan cycle so scaling happens automatically as PnL accumulates.
def compute_bet_dollars(balance):
    """Return bet size in dollars for the given balance.
       $500 balance -> $5 bets (1% of bankroll)
       $700         -> $15
       $900         -> $25
       $1100        -> $35
       $1400+       -> $50 (cap)"""
    if balance is None or balance < 500:
        return 5
    # Linear: (balance - 400) / 20 gives 5 at $500, 50 at $1400
    return max(5, min(50, (int(balance) - 400) // 20))


def compute_daily_loss_limit(bet_dollars):
    """Daily loss floor scales with bet size — otherwise a single loss auto-halts
       when scaled up. Allows ~6-8 losing bets/day of net loss before halt."""
    return max(60, bet_dollars * 6)

# Kill switches (some now dynamic)
STOP_BALANCE       = 350      # halt if balance drops below this ($150 max loss on $500 stake)
CONSEC_LOSS_LIMIT  = 5        # halt for 60 min after 5 consecutive losses
                                # (v5 has ~4% loss rate; 5-in-a-row prob = 0.04^5 = 1e-7)
MAX_POSITIONS_STATE = 500     # keep only most recent settled positions in state


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
    """Returns (halt: bool, reason: str). Daily loss limit is dynamic based on bet size."""
    if balance is not None and balance <= STOP_BALANCE:
        return True, f"balance ${balance:.2f} <= stop ${STOP_BALANCE}"
    today = datetime.now(timezone.utc).date().isoformat()
    daily = state.get("daily", {})
    bet_dollars = compute_bet_dollars(balance)
    daily_loss_limit = compute_daily_loss_limit(bet_dollars)
    if daily.get("date") == today and daily.get("pnl", 0) <= -daily_loss_limit:
        return True, f"today's P&L ${daily['pnl']:+.2f} <= -${daily_loss_limit} (bet=${bet_dollars})"
    # 60-min cooldown after N consecutive losses
    cl  = state.get("consec_losses", 0)
    ts  = state.get("last_loss_ts", 0)
    if cl >= CONSEC_LOSS_LIMIT:
        age = datetime.now(timezone.utc).timestamp() - ts
        if age < 3600:
            return True, f"{cl} consec losses, cooldown {60 - int(age/60)}min"
    return False, ""


def cleanup_state(state):
    """Prune old settled positions to keep state file small."""
    positions = state.get("positions", {})
    settled_items = [(k, v) for k, v in positions.items() if v.get("settled")]
    if len(settled_items) <= MAX_POSITIONS_STATE:
        return
    # Keep the most recent MAX_POSITIONS_STATE
    settled_items.sort(key=lambda x: x[1].get("opened_at", ""), reverse=True)
    keep_tickers = set(k for k, _ in settled_items[:MAX_POSITIONS_STATE])
    keep_tickers |= set(k for k, v in positions.items() if not v.get("settled"))
    state["positions"] = {k: v for k, v in positions.items() if k in keep_tickers}


# ── order placement ───────────────────────────────────────────────────────────

def query_actual_fill(ticker, side):
    """After placing an order, query fills to get real filled count + cost.
    Returns (total_contracts, total_cost) — both 0 if no fills yet."""
    import time
    time.sleep(1.5)   # give Kalshi a moment to process
    code, r = kalshi_get("/portfolio/fills", {"ticker": ticker, "limit": 20})
    if code != 200:
        return 0, 0
    total_ct   = 0.0
    total_cost = 0.0
    for f in r.get("fills", []):
        if f.get("ticker") != ticker: continue
        if f.get("outcome_side") != side: continue
        try:
            ct = float(f.get("count_fp", "0"))
        except Exception:
            continue
        if ct <= 0: continue
        price_str = f.get("yes_price_dollars") if side == "yes" else f.get("no_price_dollars")
        try:
            price = float(price_str or 0)
        except Exception:
            continue
        total_ct   += ct
        total_cost += ct * price
    return total_ct, total_cost


def _fresh_ask_cents(ticker, side):
    """Refetch best ask right before order placement — narrows the race window
    from scan-to-order (5-30s) down to ~200ms. Returns None on error."""
    code, r = kalshi_get(f"/markets/{ticker}")
    if code != 200:
        return None
    m = r.get("market", r)
    field = "yes_ask_dollars" if side == "yes" else "no_ask_dollars"
    raw = m.get(field)
    if raw is None:
        return None
    try:
        return int(round(float(raw) * 100))
    except Exception:
        return None


def _prior_k_candle_asks(ticker, series, side, k):
    """Fetch the ask price from the K 1-min candles immediately preceding now.
    Returns list of cents (int) for the given side (length k), or None on error.

    Gate: all K prior candles' asks (same side) must be >= PRIOR_MIN_CENTS.
    This rejects 'spike into zone' entries — where the market just jumped
    into [MIN, MAX] from a much lower price — which have systematically
    lower WR. K=2 gives 98% WR vs K=1 at 96.7% (backtest).
    """
    import time
    now_ts = int(time.time())
    # Fetch enough window to cover K prior 1-min candles plus buffer.
    window = max(300, 60 * (k + 3))
    code, r = kalshi_get(
        f"/series/{series}/markets/{ticker}/candlesticks",
        {"start_ts": now_ts - window, "end_ts": now_ts - 20, "period_interval": 1},
    )
    if code != 200:
        return None
    candles = r.get("candlesticks", [])
    if len(candles) < k:
        return None
    # Take the K most-recent candles
    candles.sort(key=lambda c: c.get("end_period_ts", 0), reverse=True)
    latest_k = candles[:k]
    out = []
    for c in latest_k:
        try:
            if side == "yes":
                out.append(int(round(float(c["yes_ask"]["close_dollars"]) * 100)))
            else:
                yes_bid = int(round(float(c["yes_bid"]["close_dollars"]) * 100))
                out.append(100 - yes_bid if yes_bid > 0 else 100)
        except (KeyError, ValueError, TypeError):
            return None
    return out


def try_trade(market, state, dry_run, balance=None):
    ticker = market.get("ticker", "")
    series = market.get("event_ticker", "").split("-")[0] or ticker.split("-")[0]
    if ticker in state.get("positions", {}):
        return  # already entered this market
    bet_dollars = compute_bet_dollars(balance)
    secs_left = market.get("_secs_left", 0)
    yes_ask   = int(round(float(market.get("yes_ask_dollars", 0) or 0) * 100))
    no_ask    = int(round(float(market.get("no_ask_dollars",  0) or 0) * 100))

    # Pick side within target band. YES-only per backtest — NO side is -EV.
    side, ask_cents = None, None
    if MIN_ASK_CENTS <= yes_ask <= MAX_ASK_CENTS:
        side, ask_cents = "yes", yes_ask
    elif not YES_ONLY and MIN_ASK_CENTS <= no_ask <= MAX_ASK_CENTS:
        side, ask_cents = "no",  no_ask
    if side is None:
        return

    # PREFLIGHT RECHECK — the scan happened seconds ago. Refetch the ask right
    # before placing to catch DOWNWARD crashes (the actual loss mechanism from
    # DOGE@82c, XRP@86c, ETH@50c live losses). A limit at ask+2 still fills
    # against ANY resting ask below it, so if the market crashed between scan
    # and order landing, we'd fill outside the WR-validated safe zone.
    fresh_ask = _fresh_ask_cents(ticker, side)
    if fresh_ask is None:
        log(f"  SKIP {ticker} — could not refetch {side} ask")
        return
    if fresh_ask < MIN_ASK_CENTS:
        log(f"  SKIP {ticker} — {side} ask crashed to {fresh_ask}c "
            f"(< {MIN_ASK_CENTS}c) between scan ({ask_cents}c) and order — "
            f"unsafe entry, underlying moved against thesis")
        return
    if fresh_ask > MAX_ASK_CENTS:
        log(f"  SKIP {ticker} — {side} ask jumped to {fresh_ask}c "
            f"(> {MAX_ASK_CENTS}c) between scan ({ask_cents}c) and order — "
            f"no longer good EV")
        return

    # PRIOR-CANDLE GATE — reject 'spike into zone' entries. Backtest shows the
    # naive strategy is EV-negative because Kalshi's ask is calibrated to true
    # probability. The subset where the market was sustainably confident (prior
    # K candles all above threshold) has WR ~98% with real edge.
    prior_asks = _prior_k_candle_asks(ticker, series, side, PRIOR_LOOKBACK)
    if prior_asks is None:
        log(f"  SKIP {ticker} — could not fetch prior candles; gate fails closed")
        return
    if any(pa < PRIOR_MIN_CENTS for pa in prior_asks):
        log(f"  SKIP {ticker} — prior {PRIOR_LOOKBACK}-min {side} asks were "
            f"{prior_asks} (need all >= {PRIOR_MIN_CENTS}c); spike-into-zone entry")
        return

    # TIGHT limit based on FRESH ask (not stale scan value). If market moves
    # >LIMIT_BUFFER cents up between refetch and Kalshi processing, we miss
    # the trade (no harm). If it moves down, the preflight above caught it.
    limit_cents = min(MAX_ASK_CENTS + LIMIT_BUFFER, fresh_ask + LIMIT_BUFFER)
    contracts   = max(1, int(bet_dollars * 100 / fresh_ask) + 1)
    est_cost    = contracts * fresh_ask / 100
    est_profit  = contracts * (100 - fresh_ask) / 100 * (1 - 0.07)

    log(f"  TRADE: {ticker}  {secs_left:.0f}s left  {side.upper()} "
        f"scan={ask_cents}c fresh={fresh_ask}c  limit={limit_cents}c  "
        f"{contracts} contracts  bet=${bet_dollars}  est.cost=${est_cost:.2f}  est.win=+${est_profit:.2f}")

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
        # After order, immediately query fills to get REAL cost + contract count.
        # This corrects for partial fills, better-than-limit prices, etc.
        actual_contracts, actual_cost = query_actual_fill(ticker, side)
        outside_safe_zone = False
        if actual_contracts > 0:
            avg_price_cents = int(round(100 * actual_cost / actual_contracts))
            log(f"    actual fill: {actual_contracts} contracts, cost ${actual_cost:.2f}, "
                f"avg={avg_price_cents}c")
            final_contracts = actual_contracts
            final_cost      = actual_cost
            # POST-FILL SAFETY — even with preflight, ~200ms race can slip a
            # bad fill through (e.g., stale resting ask well below our limit
            # gets consumed). Alert loudly so user can manually exit if needed.
            if avg_price_cents < MIN_ASK_CENTS:
                outside_safe_zone = True
                log(f"    ⚠ DANGER — filled at {avg_price_cents}c, BELOW "
                    f"safe zone [{MIN_ASK_CENTS},{MAX_ASK_CENTS}]. Preflight "
                    f"race slipped — trade has elevated loss risk.")
                send_email(
                    f"[Kalshi-C] DANGER FILL {avg_price_cents}c — {ticker}",
                    f"Filled {actual_contracts} {side.upper()} @ avg {avg_price_cents}c\n"
                    f"Safe zone: [{MIN_ASK_CENTS}, {MAX_ASK_CENTS}]c\n"
                    f"Scan saw: {ask_cents}c, refetch saw: {fresh_ask}c\n"
                    f"Fill was BELOW safe zone — market crashed in the ~200ms\n"
                    f"between refetch and order landing. Consider manual exit.\n",
                )
        else:
            log(f"    no fill data available yet — using estimates")
            final_contracts = contracts
            final_cost      = est_cost
        state["positions"][ticker] = {
            "side":        side,
            "limit_cents": limit_cents,
            "ask_at_entry": fresh_ask,
            "ask_at_scan":  ask_cents,
            "outside_safe_zone": outside_safe_zone,
            "contracts":   final_contracts,
            "cost":        final_cost,
            "opened_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "settled":     False,
        }
        state["stats"]["trades"] += 1
        # track trades per day
        today = datetime.now(timezone.utc).date().isoformat()
        daily = state.setdefault("daily", {"date": today, "pnl": 0.0, "trades_today": 0})
        if daily.get("date") != today:
            daily["date"] = today
            daily["pnl"]  = 0.0
            daily["trades_today"] = 0
        daily["trades_today"] = daily.get("trades_today", 0) + 1
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

    bet = compute_bet_dollars(balance)
    dll = compute_daily_loss_limit(bet)
    log(f"  bet_size=${bet} (balance=${balance:.2f})  daily_loss_limit=${dll}")
    n_scanned, n_tradeable = 0, 0
    for series in SERIES_LIST:
        markets = open_markets_near_close(series)
        n_scanned += len(markets)
        for m in markets:
            before = len(state.get("positions", {}))
            try_trade(m, state, dry_run, balance=balance)
            if len(state.get("positions", {})) > before:
                n_tradeable += 1

    stats = state["stats"]
    wr = stats["wins"] / stats["trades"] if stats["trades"] else 0
    log(f"  scanned {n_scanned} near-close markets, new trades: {n_tradeable}")
    log(f"  cumulative: {stats['wins']}/{stats['trades']} = {wr*100:.1f}% WR  P&L=${stats['pnl']:+.2f}")

    # Prune old settled positions to keep state file compact
    cleanup_state(state)
    save_state(state)
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
