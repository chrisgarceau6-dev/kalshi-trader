#!/usr/bin/env python3
"""Late-certainty trader v5.12 — late-certainty YES entries.

STRATEGY:
  Buy YES at a 90-93 cent ask with 150-600 seconds remaining when each of
  the two preceding 1-minute YES asks was at least 75 cents. Hold through
  settlement. UTC hour 17 is excluded. Live series are the six 15-minute
  crypto markets plus KXWTI15M; excluded candidates are shadow-logged only.

BET SIZING:
  Flat $100 principal-risk budget per order. Contract count is sized from
  the limit price so principal at the worst allowed fill cannot exceed $100.
  Exchange fees are additional.

KILL SWITCHES:
  STOP_BALANCE=$650
  trailing-24h loss limit = 8x bet size ($800 at current sizing)
  5 consecutive losses -> 60-minute cooldown
  50-trade WR below 84% -> 2-hour degradation halt
  ambiguous execution state -> persistent fail-closed halt

usage: --once | --dry-run | --status
"""

import argparse, base64, json, os, random, smtplib, time, urllib.request, urllib.error, uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from kalshi_auth import get as _get, place_order, cancel_order

COINBASE_PAIR = {
    "KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD", "KXBNB15M": "BNB-USD", "KXXRP15M": "XRP-USD",
}
HYPERLIQUID_PAIR = {}


def coinbase_1min_close(series, minute_end_ts=None):
    """Fetch the 1-minute close price from Coinbase. Returns None on any error."""
    pair = COINBASE_PAIR.get(series)
    if not pair:
        return None
    if minute_end_ts is None:
        minute_end_ts = int(time.time())
    end   = minute_end_ts
    start = end - 120
    url = (f"https://api.exchange.coinbase.com/products/{pair}/candles"
           f"?granularity=60&start={start}&end={end}")
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-v5-filter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    if not data:
        return None
    data.sort(key=lambda row: -row[0])  # [time, low, high, open, close, volume]; newest first
    return float(data[0][4])


def hyperliquid_1min_close(coin, minute_end_ts=None):
    """Fetch 1-min close from Hyperliquid for HYPE. Returns None on any error."""
    if minute_end_ts is None:
        minute_end_ts = int(time.time())
    end_ms   = minute_end_ts * 1000
    start_ms = (minute_end_ts - 120) * 1000
    body = json.dumps({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1m", "startTime": start_ms, "endTime": end_ms},
    }).encode()
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "kalshi-v5-filter/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    if not data:
        return None
    data.sort(key=lambda c: -c.get("t", 0))  # {t, o, h, l, c, v}; newest first
    try:
        return float(data[0]["c"])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def spot_1min_close(series, minute_end_ts=None):
    """Route spot price lookup to the right exchange for each series."""
    if series in COINBASE_PAIR:
        return coinbase_1min_close(series, minute_end_ts)
    if series in HYPERLIQUID_PAIR:
        return hyperliquid_1min_close(HYPERLIQUID_PAIR[series], minute_end_ts)
    return None

BASE       = Path(__file__).parent
STATE_FILE = BASE / "certainty_state.json"
LOG_FILE   = BASE / "certainty.log"

# ── strategy constants ─────────────────────────────────────────────────────────
SERIES_LIST     = [
    # 15m crypto series — v5.7 OOS-validated config:
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M",
    # WTI crude oil — added v5.8 (2026-08-13):
    # 13d backtest: 94.5% WR full, 95.5% OOS, +$1.75/trade OOS, ~13/day → +$14/day expected.
    # Gold and silver backtested same day — both negative OOS. Not added.
    "KXWTI15M",
    # Explicitly EXCLUDED:
    # - KXHYPE15M — live break-even WR 96.6% (avg loss $37), actual 95% → shadow-testing
    # - KXNEAR15M — live break-even WR 95.5% (avg loss $30), actual 92% → EV-negative (2026-08-09)
    # - KXZEC15M  — OOS test still -$3.88 (WR 93.9%)
    # - KXGOLD15M — 86.5% OOS WR, -$2.59/trade OOS. Dead.
    # - KXSILVER15M — 84.8% OOS WR, -$3.52/trade OOS. Dead.
    # - KXWTIH — reverted v5.9→v5.10 (2026-08-14): n=32 OOS insufficient (95% CI -$6.34 to +$3.45/trade,
    #   p=0.54 vs break-even); implementation had close-time grouping bug; possible regime break
    #   (KXWTIH settlement feed changed July 30 — pre/post data should not be pooled). Shadow-only pending
    #   re-backtest on post-July-30 data only with paired first/second strike comparison.
]

# Series excluded from live trading but scanned each run for shadow-test data collection.
# Shadow trades are logged with [SHADOW:reason] prefix — no orders placed.
# KXBTCD/KXETHD: hourly crypto price markets, 188 strikes/close — multi-strike candidate.
# KXWTIH: reverted to shadow — n=32 OOS insufficient, regime break, implementation bug.
SHADOW_SERIES   = ["KXHYPE15M", "KXBTCD", "KXETHD", "KXWTIH"]

STRATEGY_VERSION = "v5.12"  # order safety + remove unvalidated BTC hourly blackout

MIN_ASK_CENTS   = 90     # v5: widened entry from [95,99] to [90,99] — more volume
MAX_ASK_CENTS   = 93     # v5.6.4: lowered 95→93 to avoid partial fills at thin 94-95c book
PRIOR_MIN_CENTS = 75     # v5.6.5: relaxed 80→75c — same WR, +38% volume (filter audit Aug 10)
PRIOR_LOOKBACK  = 2      # v5.6.5: relaxed 3→2 candles — -0.1pp WR, +53% volume (filter audit Aug 10)
YES_ONLY        = True   # NO side is -EV: live 74W/8L vs YES 46W/1L (Aug 11 audit)
# TIGHT limit — small buffer above observed ask. Prevents catastrophic fills at
# way-below-ask prices (which happened in live trading when market crashed
# between scan and order-execution). If market moves >LIMIT_BUFFER cents up
# between scan and fill, we DON'T fill — we miss the trade, no harm.
LIMIT_BUFFER    = 2      # bid = ask + 2c (accepts tiny slippage, rejects worse)

# Increased from 60s -> 150s. Order placement takes 5-30s (network + Kalshi
# processing). If scan sees 61s remaining, order might land with <30s left,
# which puts us in the risky "final minute" bucket where NO has 84% WR (not 100).
MIN_SECS_LEFT   = 150    # 150-239s bucket CI is -$1.86 to +$1.57 — not confirmed negative
MAX_SECS_LEFT   = 600
BLACKOUT_HOURS  = {17}    # UTC 15 removed — ablation shows +$0.54/removed trade, no valid mechanism

# ── Longshot (crash-reversal) — OOS trial ─────────────────────────────────────
# IS (60d, 8 series, $35): 5-19c +$4,512 (+3.4-4.0pp). OOS (days 61-74):
#   5-9c: -7.5pp, 10-14c: -7.9pp — both fail OOS. 15-19c: +0.8pp — marginal hold.
#   Raised LONGSHOT_MIN_ASK 5→15 to cut the two failing buckets.
LONGSHOT_MIN_ASK   = 15
LONGSHOT_MAX_ASK   = 19
LONGSHOT_PRIOR_K   = 3
LONGSHOT_PRIOR_AVG = 60   # avg of prior K candles must be >= 60c (crash signal)
LONGSHOT_MIN_SECS  = 300
LONGSHOT_MAX_SECS  = 900
LONGSHOT_BET       = 5

# v5.5: flat bet for all series — 1.5x multiplier removed (losses on 1.5x series disproportionate)
SERIES_BET_MULTIPLIER = {}

# ── ADAPTIVE BET SIZING ────────────────────────────────────────────────────
# Flat $100 principal-risk budget per order; fees are additional.
FLAT_BET_DOLLARS = 100


def compute_bet_dollars(balance):
    return FLAT_BET_DOLLARS


def compute_daily_loss_limit(bet_dollars):
    """Trailing-24h realized-loss floor scaled to the configured bet size."""
    return max(30, bet_dollars * 8)

ROLLING_PNL_SECONDS = 86400  # trailing 24h window avoids double-limit at midnight UTC

def daily_pnl(state, now_ts=None):
    """Realized P&L over the trailing 24 hours.
    Uses settled_ts when available; falls back to settled_date == today for
    legacy positions recorded before this field existed."""
    now_ts = now_ts or datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - ROLLING_PNL_SECONDS
    today  = datetime.fromtimestamp(now_ts, tz=timezone.utc).date().isoformat()
    return round(sum(
        p.get("pnl", 0)
        for p in state.get("positions", {}).values()
        if p.get("settled") and (
            float(p["settled_ts"]) >= cutoff
            if p.get("settled_ts") else p.get("settled_date") == today
        )
    ), 2)

# Kill switches (some now dynamic)
STOP_BALANCE            = 650  # halt if balance drops below this (raised from 300 with bet bump $45→$100, 2026-08-14)
CONSEC_LOSS_LIMIT       = 5   # halt for 60 min after 5 consecutive losses
MAX_CONCURRENT_POSITIONS = 2  # correlated crypto basket: effective independent bets ~1.3 at 0.7ρ; 6×$45=$270=33% account
EDGE_DEGRADE_WINDOW     = 50  # rolling trade window for WR degradation check
EDGE_DEGRADE_THRESHOLD  = 0.84  # halt if rolling WR drops below this; 88% fired on 2-sigma variance
EDGE_DEGRADE_COOLDOWN   = 7200  # 2h: auto-clear edge degrade if consec_losses < 3 (prevents deadlock)
MAX_POSITIONS_STATE     = 500  # keep only most recent settled positions in state
ORDER_TTL_SECONDS       = 4    # server-enforced expiry is the final guard against stranded GTC orders
ORDER_RECONCILE_SECONDS = 8    # maximum time to prove the order terminal and recover exact exposure


def price_cents(raw):
    """Parse a fixed-point dollar price into exact cents without bucket rounding."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw)) * Decimal("100")
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def contracts_for_risk(bet_dollars, limit_cents):
    """Largest whole-contract count whose worst-case limit cost is <= the bet."""
    try:
        budget = Decimal(str(bet_dollars))
        price = Decimal(str(limit_cents)) / Decimal("100")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid risk sizing input") from exc
    if budget <= 0 or price <= 0:
        raise ValueError("bet and limit price must be positive")
    return max(1, int(budget / price))


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
            s = json.loads(STATE_FILE.read_text())
            if s.get("strategy_version") != STRATEGY_VERSION:
                old_version = s.get("strategy_version")
                log(f"Strategy version changed ({old_version} → {STRATEGY_VERSION}); resetting stats")
                for position in s.get("positions", {}).values():
                    if not position.get("settled"):
                        position.setdefault("strategy_version", old_version)
                s["stats"] = {"trades": 0, "wins": 0, "pnl": 0.0}
                s["recent_results"] = []
                s["strategy_version"] = STRATEGY_VERSION
            return s
        except Exception as exc:
            raise RuntimeError(f"state exists but is unreadable; fail closed: {exc}") from exc
    return {
        "positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        "recent_results": [], "strategy_version": STRATEGY_VERSION,
    }


def save_state(s):
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(s, indent=2, default=str))
    os.replace(temporary, STATE_FILE)


def log(msg):
    ts   = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def fetch_balance():
    code, resp = kalshi_get("/portfolio/balance")
    if code != 200:
        log(f"  fetch_balance: HTTP {code} resp={str(resp)[:120]}")
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


# ── shadow test logging ────────────────────────────────────────────────────────

def shadow_log(reason, ticker, side, ask, secs_left):
    """Log a hypothetical trade that was excluded. Builds shadow-test dataset passively.
    Parse these lines later with grep '[SHADOW:' on workflow logs."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"  [SHADOW:{reason}] {ticker}  {side.upper()}  {ask}c  {secs_left:.0f}s  ts={now}")


# ── market scanning ────────────────────────────────────────────────────────────

def open_markets_near_close(series):
    """Return open markets for series with 60-900s remaining."""
    # KXBTCD/KXETHD have 100+ strikes per close time — need higher limit to capture all qualifying
    lim = 100 if series in ("KXBTCD", "KXETHD") else 10
    code, r = kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": lim})
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
        if close_dt.hour in BLACKOUT_HOURS:
            continue
        if MIN_SECS_LEFT <= secs <= MAX_SECS_LEFT:
            m["_secs_left"] = secs
            out.append(m)
    return out


def open_markets_longshot(series):
    """Return open markets for series with 300-900s remaining (longshot window)."""
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
        if close_dt.hour in BLACKOUT_HOURS:
            continue
        if LONGSHOT_MIN_SECS <= secs <= LONGSHOT_MAX_SECS:
            m["_secs_left"] = secs
            out.append(m)
    return out


# ── kill switches ──────────────────────────────────────────────────────────────

def check_halts(state, balance):
    """Returns (halt: bool, reason: str). Daily loss limit is dynamic based on bet size."""
    if state.get("execution_halt_reason"):
        return True, f"execution safety halt: {state['execution_halt_reason']}"
    if balance is not None and balance <= STOP_BALANCE:
        return True, f"balance ${balance:.2f} <= stop ${STOP_BALANCE}"
    bet_dollars = compute_bet_dollars(balance)
    daily_loss_limit = compute_daily_loss_limit(bet_dollars)
    d_pnl = daily_pnl(state)
    if d_pnl <= -daily_loss_limit:
        return True, f"today's P&L -{abs(d_pnl):.2f} <= -${daily_loss_limit} (bet=${bet_dollars})"
    # 60-min cooldown after N consecutive losses
    cl  = state.get("consec_losses", 0)
    ts  = state.get("last_loss_ts", 0)
    if cl >= CONSEC_LOSS_LIMIT:
        age = datetime.now(timezone.utc).timestamp() - ts
        if age < 3600:
            return True, f"{cl} consec losses, cooldown {60 - int(age/60)}min"
    # Rolling WR degradation check — with 2h auto-recovery to prevent permanent deadlock
    recent = state.get("recent_results", [])
    if len(recent) >= EDGE_DEGRADE_WINDOW:
        window = recent[-EDGE_DEGRADE_WINDOW:]
        rolling_wr = sum(r[0] for r in window) / EDGE_DEGRADE_WINDOW
        if rolling_wr < EDGE_DEGRADE_THRESHOLD:
            halted_at = state.get("edge_degrade_halted_at", 0)
            now_ts = datetime.now(timezone.utc).timestamp()
            cl = state.get("consec_losses", 0)
            if halted_at == 0:
                state["edge_degrade_halted_at"] = now_ts
                halted_at = now_ts
            age = now_ts - halted_at
            if age >= EDGE_DEGRADE_COOLDOWN and cl < 3:
                state["edge_degrade_halted_at"] = 0
                state["recent_results"] = []
                log("  edge degrade cooldown expired — clearing window and resuming")
            else:
                return True, (f"edge degrade: rolling {EDGE_DEGRADE_WINDOW}-trade WR "
                              f"{rolling_wr*100:.1f}% < {EDGE_DEGRADE_THRESHOLD*100:.0f}% "
                              f"(auto-recover in {max(0, int((EDGE_DEGRADE_COOLDOWN - age)/60))}min)")
        else:
            state["edge_degrade_halted_at"] = 0
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

def fetch_live_position_tickers():
    """Return set of tickers with unsettled Kalshi positions. One call per run."""
    tickers = set()
    cursor = None
    while True:
        params = {"settlement_status": "unsettled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        code, r = kalshi_get("/portfolio/positions", params)
        if code != 200 or not isinstance(r, dict):
            return None
        tickers.update(
            p.get("ticker") for p in r.get("market_positions", []) if p.get("ticker")
        )
        next_cursor = r.get("cursor")
        if not next_cursor or next_cursor == cursor:
            return tickers
        cursor = next_cursor


def fetch_resting_order_tickers():
    """Return one ticker entry per resting order, preserving duplicate exposure."""
    tickers = []
    cursor = None
    while True:
        params = {"status": "resting", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        code, r = kalshi_get("/portfolio/orders", params)
        if code != 200 or not isinstance(r, dict):
            return None
        tickers.extend(o.get("ticker") for o in r.get("orders", []) if o.get("ticker"))
        next_cursor = r.get("cursor")
        if not next_cursor or next_cursor == cursor:
            return tickers
        cursor = next_cursor


def query_actual_fill(ticker, side, order_id=None):
    """Return (contracts, cost, fees) for this order.
    Passes order_id to the API when available; client-side filter as belt-and-suspenders."""
    params = {"ticker": ticker, "limit": 1000}
    if order_id:
        params["order_id"] = order_id
    code, r = kalshi_get("/portfolio/fills", params)
    if code != 200:
        return None
    total_ct = total_cost = total_fee = 0.0
    for f in r.get("fills", []):
        if f.get("ticker") != ticker: continue
        if f.get("outcome_side") != side: continue
        if order_id and f.get("order_id") != order_id: continue
        try:
            ct = float(f.get("count_fp", "0") or 0)
        except Exception:
            continue
        if ct <= 0: continue
        price_field = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        try:
            price = float(f.get(price_field, "0") or 0)
            fee   = float(f.get("fee_cost", "0") or 0)
        except Exception:
            continue
        total_ct   += ct
        total_cost += ct * price
        total_fee  += fee
    return total_ct, total_cost, total_fee


def query_order(order_id):
    """Return the authoritative order record, or None on an API/read error."""
    code, r = kalshi_get(f"/portfolio/orders/{order_id}")
    if code != 200 or not isinstance(r, dict):
        return None
    order = r.get("order", r)
    return order if isinstance(order, dict) else None


def reconcile_terminal_order(order_id, ticker, side):
    """Prove an order is terminal and return exact (contracts, cost, fees).

    The order record is authoritative for total fill count/cost, which avoids
    undercounting fills that propagate after the first fills-API query.
    """
    deadline = time.monotonic() + ORDER_RECONCILE_SECONDS
    last_order = None
    while time.monotonic() < deadline:
        order = query_order(order_id)
        if order is not None:
            last_order = order
            try:
                remaining = Decimal(str(order.get("remaining_count_fp", "0") or "0"))
                filled = Decimal(str(order.get("fill_count_fp", "0") or "0"))
            except (InvalidOperation, TypeError, ValueError):
                remaining = Decimal("-1")
                filled = Decimal("-1")
            status = str(order.get("status", "")).lower()
            if remaining == 0 and status != "resting" and filled >= 0:
                try:
                    cost = (
                        Decimal(str(order.get("taker_fill_cost_dollars", "0") or "0"))
                        + Decimal(str(order.get("maker_fill_cost_dollars", "0") or "0"))
                    )
                    fees = (
                        Decimal(str(order.get("taker_fees_dollars", "0") or "0"))
                        + Decimal(str(order.get("maker_fees_dollars", "0") or "0"))
                    )
                except (InvalidOperation, TypeError, ValueError):
                    cost = fees = Decimal("-1")
                if filled == 0:
                    return 0.0, 0.0, 0.0
                # A terminal filled order cannot genuinely cost $0. Treat that
                # as an eventually-consistent record and keep reconciling.
                if cost > 0 and fees >= 0:
                    return float(filled), float(cost), float(fees)

                fill_totals = query_actual_fill(ticker, side, order_id)
                if (
                    fill_totals is not None
                    and fill_totals[1] > 0
                    and abs(fill_totals[0] - float(filled)) < 1e-6
                ):
                    return fill_totals
        time.sleep(0.5)

    status = last_order.get("status") if last_order else "unavailable"
    remaining = last_order.get("remaining_count_fp") if last_order else "unknown"
    raise RuntimeError(
        f"order reconciliation unresolved for {order_id}: status={status}, remaining={remaining}"
    )


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
    return price_cents(raw)


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
                ask = price_cents(c["yes_ask"]["close_dollars"])
                if ask is None:
                    return None
                out.append(ask)
            else:
                yes_bid = price_cents(c["yes_bid"]["close_dollars"])
                if yes_bid is None:
                    return None
                out.append(Decimal("100") - yes_bid if yes_bid > 0 else Decimal("100"))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return None
    return out


def try_trade(
    market,
    state,
    dry_run,
    balance=None,
    live_position_tickers=None,
    resting_order_tickers=None,
):
    ticker = market.get("ticker", "")
    series = market.get("event_ticker", "").split("-")[0] or ticker.split("-")[0]
    live_position_tickers = set(live_position_tickers or ())
    resting_order_tickers = list(resting_order_tickers or ())
    if ticker in state.get("positions", {}):
        return  # already entered this market
    if ticker in live_position_tickers or ticker in resting_order_tickers:
        log(f"  SKIP {ticker} — live Kalshi position/order exists (state was stale)")
        return
    state_open = {
        t for t, p in state.get("positions", {}).items() if not p.get("settled")
    }
    # Kalshi positions are aggregated by ticker, while every resting order is
    # separate potential exposure and must consume its own concurrency slot.
    open_cnt = len(state_open | live_position_tickers) + len(resting_order_tickers)
    if open_cnt >= MAX_CONCURRENT_POSITIONS:
        log(f"  SKIP {ticker} — heat check: {open_cnt} open positions (limit {MAX_CONCURRENT_POSITIONS})")
        return
    bet_dollars = compute_bet_dollars(balance)
    secs_left = market.get("_secs_left", 0)
    yes_ask   = price_cents(market.get("yes_ask_dollars"))
    no_ask    = price_cents(market.get("no_ask_dollars"))
    yes_ask   = yes_ask if yes_ask is not None else Decimal("-1")
    no_ask    = no_ask if no_ask is not None else Decimal("-1")

    # Pick side within target band. YES-only per backtest — NO side is -EV.
    side, ask_cents = None, None
    if MIN_ASK_CENTS <= yes_ask <= MAX_ASK_CENTS:
        side, ask_cents = "yes", yes_ask
    elif not YES_ONLY and MIN_ASK_CENTS <= no_ask <= MAX_ASK_CENTS:
        side, ask_cents = "no",  no_ask
    # Shadow-log NO 90-91c when YES_ONLY blocks it — these are the strongest NO
    # candidates in backtest (+$0.73/trade); tracking for future re-entry decision.
    if YES_ONLY and side is None and 90 <= no_ask <= 91:
        shadow_log("NO-90-91", ticker, "no", no_ask, secs_left)
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

    # H4 and near-strike filters removed 2026-08-12:
    # 60-day ablation showed both are net-negative P&L (remove +$431 and +$28
    # respectively) with no statistical case for tail-risk benefit at this bet size.

    # TIGHT limit based on FRESH ask. Cap at MAX_ASK_CENTS so slippage can't
    # push us above the OOS-validated range (96c+ is EV-negative after fee).
    limit_cents = min(Decimal(MAX_ASK_CENTS), fresh_ask + Decimal(LIMIT_BUFFER))
    contracts   = contracts_for_risk(bet_dollars, limit_cents)
    est_cost    = float(Decimal(contracts) * fresh_ask / Decimal("100"))
    max_cost    = float(Decimal(contracts) * limit_cents / Decimal("100"))
    est_profit  = float(Decimal(contracts) * (Decimal("100") - fresh_ask) / Decimal("100") * Decimal("0.93"))

    log(f"  TRADE: {ticker}  {secs_left:.0f}s left  {side.upper()} "
        f"scan={ask_cents}c fresh={fresh_ask}c  limit={limit_cents}c  "
        f"{contracts} contracts  bet=${bet_dollars}  est.cost=${est_cost:.2f}  "
        f"max.cost=${max_cost:.2f}  est.win=+${est_profit:.2f}")

    if dry_run:
        log(f"    [dry-run] skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        log(f"    KALSHI_API_KEY_ID not set — cannot trade")
        return

    client_order_id = str(uuid.uuid4())
    expiration_time = int(time.time()) + ORDER_TTL_SECONDS
    code, resp = place_order(
        ticker, side, contracts,
        yes_price_cents=limit_cents if side == "yes" else None,
        no_price_cents=limit_cents  if side == "no"  else None,
        time_in_force="good_till_canceled",
        expiration_time=expiration_time,
        client_order_id=client_order_id,
    )
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("order_id") or resp.get("order", {}).get("order_id")
    if code in (200, 201):
        if not order_id:
            reason = f"accepted {ticker} order had no order_id; manual account reconciliation required"
            state["execution_halt_reason"] = reason
            save_state(state)
            log(f"    DANGER — {reason}; server expiry={expiration_time}")
            send_email(f"[Kalshi-C] EXECUTION HALT — {ticker}", reason)
            raise RuntimeError(reason)
        log(f"    order accepted (id={order_id})")
        time.sleep(3)  # wait for fills to propagate before cancelling
        # Cancel GTC FIRST so the fill picture is final when we query.
        c_code, _ = cancel_order(order_id)
        log(f"    cancel GTC order {order_id} (HTTP {c_code})")
        if c_code not in (200, 204, 404):
            log(f"    WARNING — cancel returned HTTP {c_code}; waiting for server expiry")
        try:
            actual_contracts, actual_cost, actual_fee = reconcile_terminal_order(order_id, ticker, side)
        except RuntimeError as exc:
            reason = f"{ticker}: {exc}"
            state["execution_halt_reason"] = reason
            save_state(state)
            send_email(f"[Kalshi-C] EXECUTION HALT — {ticker}", reason)
            raise
        outside_safe_zone = False
        if actual_contracts > 0:
            avg_price_cents = Decimal(str(actual_cost)) / Decimal(str(actual_contracts)) * Decimal("100")
            contracts_intended = contracts
            if actual_contracts < contracts_intended * 0.9:
                log(f"    PARTIAL FILL: {actual_contracts}/{contracts_intended} contracts "
                    f"(${actual_cost:.2f} vs ${est_cost:.2f} expected)")
            log(f"    actual fill: {actual_contracts} contracts, cost ${actual_cost:.2f}, "
                f"avg={avg_price_cents:.2f}c  fee=${actual_fee:.4f}")
            final_contracts = actual_contracts
            final_cost      = actual_cost
            final_fee       = actual_fee
            # POST-FILL SAFETY — even with preflight, ~200ms race can slip a
            # bad fill through (e.g., stale resting ask well below our limit
            # gets consumed). Alert loudly so user can manually exit if needed.
            if avg_price_cents < MIN_ASK_CENTS:
                outside_safe_zone = True
                log(f"    DANGER — filled at {avg_price_cents}c, BELOW "
                    f"safe zone [{MIN_ASK_CENTS},{MAX_ASK_CENTS}]. Preflight "
                    f"race slipped — trade has elevated loss risk.")
                send_email(
                    f"[Kalshi-C] DANGER FILL {avg_price_cents}c — {ticker}",
                    f"Filled {actual_contracts} {side.upper()} @ avg {avg_price_cents:.2f}c\n"
                    f"Safe zone: [{MIN_ASK_CENTS}, {MAX_ASK_CENTS}]c\n"
                    f"Scan saw: {ask_cents}c, refetch saw: {fresh_ask}c\n"
                    f"Fill was BELOW safe zone — market crashed in the ~200ms\n"
                    f"between refetch and order landing. Consider manual exit.\n",
                )
        else:
            log(f"    no fill after 3s — order already cancelled above")
            return  # don't record a phantom position
        state["positions"][ticker] = {
            "side":        side,
            "limit_cents": float(limit_cents),
            "ask_at_entry": float(fresh_ask),
            "ask_at_scan":  float(ask_cents),
            "outside_safe_zone": outside_safe_zone,
            "contracts":   final_contracts,
            "cost":        final_cost,
            "fee_cost":    final_fee,
            "order_id":    order_id,
            "strategy_version": STRATEGY_VERSION,
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
    else:
        log(f"    order FAILED — HTTP {code}: {str(resp)[:200]}")


def try_longshot_trade(market, state, dry_run):
    ticker = market.get("ticker", "")
    series = market.get("event_ticker", "").split("-")[0] or ticker.split("-")[0]
    if ticker in state.get("positions", {}):
        return
    open_cnt = sum(1 for p in state.get("positions", {}).values() if not p.get("settled"))
    if open_cnt >= MAX_CONCURRENT_POSITIONS:
        return

    secs_left = market.get("_secs_left", 0)
    yes_ask   = price_cents(market.get("yes_ask_dollars"))
    no_ask    = price_cents(market.get("no_ask_dollars"))
    yes_ask   = yes_ask if yes_ask is not None else Decimal("-1")
    no_ask    = no_ask if no_ask is not None else Decimal("-1")

    side, ask_cents = None, None
    if LONGSHOT_MIN_ASK <= yes_ask <= LONGSHOT_MAX_ASK:
        side, ask_cents = "yes", yes_ask
    elif LONGSHOT_MIN_ASK <= no_ask <= LONGSHOT_MAX_ASK:
        side, ask_cents = "no", no_ask
    if side is None:
        return

    prior_asks = _prior_k_candle_asks(ticker, series, side, LONGSHOT_PRIOR_K)
    if prior_asks is None or len(prior_asks) < LONGSHOT_PRIOR_K:
        return
    prior_avg = sum(prior_asks) / len(prior_asks)
    if prior_avg < LONGSHOT_PRIOR_AVG:
        return

    fresh_ask = _fresh_ask_cents(ticker, side)
    if fresh_ask is None or not (LONGSHOT_MIN_ASK <= fresh_ask <= LONGSHOT_MAX_ASK):
        return

    limit_cents = min(Decimal(LONGSHOT_MAX_ASK), fresh_ask + Decimal(LIMIT_BUFFER))
    contracts   = contracts_for_risk(LONGSHOT_BET, limit_cents)
    est_cost    = float(Decimal(contracts) * fresh_ask / Decimal("100"))
    est_profit  = float(Decimal(contracts) * (Decimal("100") - fresh_ask) / Decimal("100") * Decimal("0.93"))

    log(f"  LONGSHOT: {ticker}  {secs_left:.0f}s left  {side.upper()} "
        f"scan={ask_cents}c fresh={fresh_ask}c prior_avg={prior_avg:.0f}c  "
        f"limit={limit_cents}c  {contracts} contracts  bet=${LONGSHOT_BET}  "
        f"est.win=+${est_profit:.2f}")

    if dry_run:
        log(f"    [dry-run] skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        return

    client_order_id = str(uuid.uuid4())
    expiration_time = int(time.time()) + ORDER_TTL_SECONDS
    code, resp = place_order(
        ticker, side, contracts,
        yes_price_cents=limit_cents if side == "yes" else None,
        no_price_cents=limit_cents  if side == "no"  else None,
        time_in_force="good_till_canceled",
        expiration_time=expiration_time,
        client_order_id=client_order_id,
    )
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("order_id") or resp.get("order", {}).get("order_id")
    if code in (200, 201):
        if not order_id:
            reason = f"accepted {ticker} longshot order had no order_id; manual reconciliation required"
            state["execution_halt_reason"] = reason
            save_state(state)
            raise RuntimeError(reason)
        log(f"    order accepted (id={order_id})")
        time.sleep(3)
        c_code, _ = cancel_order(order_id)
        log(f"    cancel GTC order {order_id} (HTTP {c_code})")
        try:
            actual_contracts, actual_cost, actual_fee = reconcile_terminal_order(order_id, ticker, side)
        except RuntimeError as exc:
            state["execution_halt_reason"] = f"{ticker}: {exc}"
            save_state(state)
            raise
        if actual_contracts == 0:
            log(f"    no fill")
            return
        avg_price_cents = Decimal(str(actual_cost)) / Decimal(str(actual_contracts)) * Decimal("100")
        log(f"    actual fill: {actual_contracts} contracts, cost ${actual_cost:.2f}, avg={avg_price_cents:.2f}c  fee=${actual_fee:.4f}")
        state["positions"][ticker] = {
            "side":         side,
            "limit_cents":  float(limit_cents),
            "ask_at_entry": float(fresh_ask),
            "ask_at_scan":  float(ask_cents),
            "outside_safe_zone": False,
            "contracts":    actual_contracts,
            "cost":         actual_cost,
            "fee_cost":     actual_fee,
            "order_id":     order_id,
            "strategy_version": STRATEGY_VERSION,
            "opened_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "settled":      False,
            "strategy":     "longshot",
        }
        ls = state.setdefault("longshot_stats", {"trades": 0, "wins": 0, "pnl": 0.0})
        ls["trades"] += 1
        today = datetime.now(timezone.utc).date().isoformat()
        daily = state.setdefault("daily", {"date": today, "pnl": 0.0, "trades_today": 0})
        if daily.get("date") != today:
            daily["date"] = today
            daily["pnl"]  = 0.0
            daily["trades_today"] = 0
        daily["trades_today"] = daily.get("trades_today", 0) + 1
        save_state(state)
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

        won    = (result == pos["side"])
        payout = float(pos["contracts"]) if won else 0.0
        cost   = float(pos["cost"])
        if "fee_cost" in pos:
            fee = float(pos["fee_cost"])
        else:
            contracts  = float(pos["contracts"])
            avg_price  = cost / contracts if contracts else 0
            fee = round(0.07 * contracts * avg_price * (1 - avg_price), 4)
        pnl = round(payout - cost - fee, 2)

        pos["settled"]      = True
        pos["settled_ts"]   = datetime.now(timezone.utc).timestamp()
        pos["result"]       = result
        pos["pnl"]          = pnl
        pos["settled_date"] = today  # retained for backward compat

        daily["pnl"] = round(daily.get("pnl", 0.0) + pnl, 2)
        d_pnl  = daily_pnl(state)
        d_sign = '+' if d_pnl >= 0 else '-'

        if pos.get("strategy") == "longshot":
            ls = state.setdefault("longshot_stats", {"trades": 0, "wins": 0, "pnl": 0.0})
            ls["pnl"] = round(ls.get("pnl", 0.0) + pnl, 2)
            if won:
                ls["wins"] = ls.get("wins", 0) + 1
                state["consec_losses"] = 0
            else:
                state["consec_losses"] = state.get("consec_losses", 0) + 1
                state["last_loss_ts"]  = datetime.now(timezone.utc).timestamp()
            ls_wr = ls["wins"] / ls["trades"] * 100 if ls["trades"] else 0
            save_state(state)
            log(f"  SETTLED(LS) {ticker} result={result.upper()}  side={pos['side'].upper()}  "
                f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  LS WR={ls_wr:.1f}%")
        else:
            if won:
                state["consec_losses"] = 0
            else:
                state["consec_losses"] = state.get("consec_losses", 0) + 1
                state["last_loss_ts"]  = datetime.now(timezone.utc).timestamp()
            position_version = pos.get("strategy_version", STRATEGY_VERSION)
            if position_version == STRATEGY_VERSION:
                state["stats"]["pnl"] = round(state["stats"].get("pnl", 0.0) + pnl, 2)
                if won:
                    state["stats"]["wins"] = state["stats"].get("wins", 0) + 1
                recent = state.setdefault("recent_results", [])
                recent.append([int(won), pos.get("limit_cents", 92)])
                state["recent_results"] = recent[-EDGE_DEGRADE_WINDOW * 2:]
            save_state(state)
            wr = state["stats"]["wins"] / state["stats"]["trades"] if state["stats"]["trades"] else 0
            suffix = (
                f"cumul WR={wr*100:.1f}%"
                if position_version == STRATEGY_VERSION
                else f"carried from {position_version}; excluded from {STRATEGY_VERSION} stats"
            )
            log(f"  SETTLED {ticker} result={result.upper()}  side={pos['side'].upper()}  "
                f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  {suffix}")


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
        save_state(state)  # persist edge_degrade_halted_at so 2h cooldown actually counts down
        return

    bet = compute_bet_dollars(balance)
    dll = compute_daily_loss_limit(bet)
    log(f"  bet_size=${bet} (flat)  balance=${balance:.2f}  daily_loss_limit=${dll}")
    live_positions = fetch_live_position_tickers()
    resting_order_tickers = fetch_resting_order_tickers()
    if live_positions is None or resting_order_tickers is None:
        log("  WARNING: cannot prove live positions/resting orders — skipping this cycle")
        return
    n_scanned, n_tradeable = 0, 0
    for series in random.sample(SERIES_LIST, len(SERIES_LIST)):
        markets = open_markets_near_close(series)
        n_scanned += len(markets)
        for m in markets:
            before = len(state.get("positions", {}))
            try_trade(
                m,
                state,
                dry_run,
                balance=balance,
                live_position_tickers=live_positions,
                resting_order_tickers=resting_order_tickers,
            )
            if len(state.get("positions", {})) > before:
                n_tradeable += 1

    # Shadow scan — no orders placed, just logging for future re-entry analysis
    for series in SHADOW_SERIES:
        for m in open_markets_near_close(series):
            yes_ask = price_cents(m.get("yes_ask_dollars"))
            if yes_ask is not None and MIN_ASK_CENTS <= yes_ask <= MAX_ASK_CENTS:
                shadow_log(f"EXCL-{series}", m.get("ticker",""), "yes", yes_ask, m.get("_secs_left", 0))

    # Shadow log 600-700s window for OOS validation of MAX_SECS_LEFT=700 candidate
    for series in SERIES_LIST:
        code, r = kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 10})
        if code != 200:
            continue
        now = datetime.now(timezone.utc)
        for m in r.get("markets", []):
            ct = m.get("close_time", "")
            if not ct:
                continue
            try:
                close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                secs = (close_dt - now).total_seconds()
            except Exception:
                continue
            if close_dt.hour in BLACKOUT_HOURS:
                continue
            if not (600 < secs <= 700):
                continue
            yes_ask = price_cents(m.get("yes_ask_dollars"))
            if yes_ask is not None and MIN_ASK_CENTS <= yes_ask <= MAX_ASK_CENTS:
                shadow_log("600-700s", m.get("ticker", ""), "yes", yes_ask, secs)

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
    ap.add_argument("--daemon",  action="store_true",
                    help="run continuously, polling every 20s (for VPS deployment)")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(load_state(), indent=2))
        return
    if a.daemon:
        log("=== DAEMON MODE — polling every 20s ===")
        while True:
            try:
                run_once(dry_run=a.dry_run)
            except KeyboardInterrupt:
                log("Daemon stopped")
                break
            except Exception as e:
                log(f"cycle error: {e}")
            time.sleep(20)
        return
    try:
        run_once(dry_run=a.dry_run)
    except Exception as e:
        log(f"FATAL: {e}")
        raise


if __name__ == "__main__":
    main()
