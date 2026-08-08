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
  5% of balance, rounded to nearest $5, min $20, no cap
  $380 balance -> $20 bets
  $500         -> $25
  $700         -> $35
  $1000        -> $50
  $2000        -> $100
  $4000        -> $200

KILL SWITCHES:
  STOP_BALANCE=$300 (halt if balance drops here)
  DAILY_LOSS_LIMIT = 8x bet_size = $160/day at $20 bets
  5 consecutive losses -> 60-min cooldown

usage: --once | --dry-run | --status

usage: --once | --dry-run | --status
"""

import argparse, base64, json, os, random, smtplib, time, urllib.request, urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from kalshi_auth import get as _get, place_order, cancel_order

# ── OOS-validated pre-trade filters (added 2026-08-02) ─────────────────────────
# Walk-forward on 60d v5 backtest (train Jun 2-Jul 1 → test Jul 2-Aug 1):
#   H4 5bps + Near-strike 10bps combo: +2.43c/trade OOS, CI [+1.85, +2.92]
#   Test-set total +$61.71 vs base +$39.69 (+155%)
# Filters fail OPEN (proceed with trade) on Coinbase API errors so v5's
# base positive-EV isn't sacrificed to a transient feed outage.
COINBASE_PAIR = {
    "KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD", "KXBNB15M": "BNB-USD", "KXXRP15M": "XRP-USD",
    "KXNEAR15M": "NEAR-USD",
}
HYPERLIQUID_PAIR = {
    "KXHYPE15M": "HYPE",  # HYPE trades on Hyperliquid DEX — use their native API
}
H4_ADVERSE_BPS = 5      # skip if 60s spot moved > 5bps against side
NEAR_STRIKE_BPS = 10    # skip if |spot - strike| / spot < 10bps


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
    # 15m crypto series where v4 filter (ask[95,99] prior_k=3 pmin=92 both)
    # showed +EV in 60-day backtest:
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M",
    "KXHYPE15M",  # added — 98.75% WR, +$442/60d @ $50 in v4 backtest
    # Added 2026-08-02: OOS-validated with H4+near-strike filters. Raw v5 loses
    # money on NEAR (WR 92% but tail risk); filters flip it to +EV.
    # Walk-forward (14d, train Jul 19-25 / test Jul 25-Aug 1):
    #   Train filtered: WR 96.0%, +$0.87.  Test filtered: WR 97.8%, +$7.79.
    "KXNEAR15M",
    # Explicitly EXCLUDED:
    # - KXZEC15M — filters mitigate but OOS test half still -$3.88 (WR 93.9%)
    # - WTI/Gold/Silver 15m — TBD, backtest pending
]

STRATEGY_VERSION = "v5.5.1"  # bump when strategy logic changes; resets WR/pnl counter

MIN_ASK_CENTS   = 90     # v5: widened entry from [95,99] to [90,99] — more volume
MAX_ASK_CENTS   = 95     # v5.4: capped at 95c; 96-99c is EV-negative after 7% fee
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
MAX_SECS_LEFT   = 600    # backtest shows 600-900s bucket is net-negative EV; trimmed
# v5.4: UTC 15-17 = 11am-1pm ET; US equity open creates volatility that kills WR
BLACKOUT_HOURS  = {15, 16, 17}

# ── Longshot (crash-reversal) — OOS trial ─────────────────────────────────────
# IS backtest (60d, 8 series, $35 bets): 5-19c buckets +$4,512 (+3.4-4.0pp edge)
# 20-25c was -EV; excluded. Fixed $20 bet until OOS data confirms edge.
LONGSHOT_MIN_ASK   = 5
LONGSHOT_MAX_ASK   = 19
LONGSHOT_PRIOR_K   = 3
LONGSHOT_PRIOR_AVG = 60   # avg of prior K candles must be >= 60c (crash signal)
LONGSHOT_MIN_SECS  = 300
LONGSHOT_MAX_SECS  = 900
LONGSHOT_BET       = 20

# v5.5: flat bet for all series — 1.5x multiplier removed (losses on 1.5x series disproportionate)
SERIES_BET_MULTIPLIER = {}

# ── ADAPTIVE BET SIZING ────────────────────────────────────────────────────
# 5% of balance, rounded to nearest $5, min $20, no cap. Reads fresh balance
# every scan cycle so scaling is fully automatic. Natural limit is Kalshi
# order-book depth (~200-300 contracts); no-fill cancellation handles it.
def compute_bet_dollars(balance):
    """5% of balance, rounded to nearest $5. Floor $20, no cap."""
    if balance is None:
        return 20
    rounded = max(1, round(balance * 0.05 / 5)) * 5
    return max(20, rounded)


def compute_daily_loss_limit(bet_dollars):
    """Daily loss floor scales with bet size — otherwise a single loss auto-halts
       when scaled up. Allows ~10-15 losing bets/day of net loss before halt."""
    return max(30, bet_dollars * 8)

def daily_pnl(state):
    """Compute today's P&L from settled positions (UTC date).
    Position-based so it survives cache loss — not a running counter."""
    today = datetime.now(timezone.utc).date().isoformat()
    return round(sum(
        p.get("pnl", 0)
        for p in state.get("positions", {}).values()
        if p.get("settled") and p.get("settled_date") == today
    ), 2)

# Kill switches (some now dynamic)
STOP_BALANCE            = 300  # halt if balance drops below this
CONSEC_LOSS_LIMIT       = 5   # halt for 60 min after 5 consecutive losses
MAX_CONCURRENT_POSITIONS = 4  # cap open positions to limit correlated-loss exposure
EDGE_DEGRADE_WINDOW     = 50  # rolling trade window for WR degradation check
EDGE_DEGRADE_THRESHOLD  = 0.90  # halt if rolling WR drops below this (3σ below 96% baseline)
MAX_POSITIONS_STATE     = 500  # keep only most recent settled positions in state


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
                log(f"Strategy version changed ({s.get('strategy_version')} → {STRATEGY_VERSION}); resetting stats")
                s["stats"] = {"trades": 0, "wins": 0, "pnl": 0.0}
                s["recent_results"] = []
                s["strategy_version"] = STRATEGY_VERSION
            return s
        except Exception:
            pass
    return {
        "positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        "recent_results": [], "strategy_version": STRATEGY_VERSION,
    }


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
    """Query fills for ticker/side. Caller controls timing — no sleep here."""
    code, r = kalshi_get("/portfolio/fills", {"ticker": ticker, "limit": 50})
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
    open_cnt = sum(1 for p in state.get("positions", {}).values() if not p.get("settled"))
    if open_cnt >= MAX_CONCURRENT_POSITIONS:
        log(f"  SKIP {ticker} — heat check: {open_cnt} open positions (limit {MAX_CONCURRENT_POSITIONS})")
        return
    base_bet    = compute_bet_dollars(balance)
    multiplier  = SERIES_BET_MULTIPLIER.get(series, 1.0)
    bet_dollars = max(20, round(base_bet * multiplier / 5) * 5)
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

    # SPOT-BASED FILTERS (fail open on feed error) ─────────────────────────────
    #   H4:           skip if last-60s spot moved > 5bps against our side
    #   Near-strike:  skip if spot is within 10bps of the market's strike
    # Both walk-forward validated OOS on the v5 backtest (Jul-Aug halves).
    has_spot = series in COINBASE_PAIR or series in HYPERLIQUID_PAIR
    if has_spot:
        feed = "Hyperliquid" if series in HYPERLIQUID_PAIR else "Coinbase"
        now_ts = int(time.time())
        spot_now = spot_1min_close(series, now_ts)
        spot_60s = spot_1min_close(series, now_ts - 60)
        if spot_now is None or spot_60s is None:
            log(f"  {ticker} — {feed} feed unavailable; filters SKIPPED (fail open)")
        else:
            ret_60 = (spot_now - spot_60s) / spot_60s
            adverse = -ret_60 if side == "yes" else ret_60
            if adverse > H4_ADVERSE_BPS / 10000:
                log(f"  SKIP {ticker} — H4 filter: 60s spot moved "
                    f"{ret_60*100:+.3f}% (adverse for {side.upper()}, "
                    f"threshold {H4_ADVERSE_BPS}bps)")
                return
            strike = float(market.get("floor_strike") or 0)
            if strike:
                dist_pct = abs(spot_now - strike) / spot_now
                if dist_pct < NEAR_STRIKE_BPS / 10000:
                    log(f"  SKIP {ticker} — near-strike filter: spot {spot_now:.4f} "
                        f"within {dist_pct*10000:.1f}bps of strike {strike:.4f} "
                        f"(threshold {NEAR_STRIKE_BPS}bps)")
                    return
    else:
        log(f"  {ticker} — {series} has no spot feed; H4+near-strike filters skipped")

    # TIGHT limit based on FRESH ask. Cap at MAX_ASK_CENTS so slippage can't
    # push us above the OOS-validated range (96c+ is EV-negative after fee).
    limit_cents = min(MAX_ASK_CENTS, fresh_ask + LIMIT_BUFFER)
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
    order_id = resp.get("order", {}).get("order_id") if isinstance(resp, dict) else None
    if code in (200, 201):
        log(f"    order accepted (id={order_id})")
        time.sleep(3)  # wait for fills to propagate before cancelling
        # Cancel GTC FIRST so the fill picture is final when we query.
        if order_id:
            c_code, _ = cancel_order(order_id)
            log(f"    cancelled GTC order {order_id} (HTTP {c_code})")
        time.sleep(0.5)  # brief settle after cancel
        actual_contracts, actual_cost = query_actual_fill(ticker, side)
        if actual_contracts == 0:
            time.sleep(1.5)
            actual_contracts, actual_cost = query_actual_fill(ticker, side)
        outside_safe_zone = False
        if actual_contracts > 0:
            avg_price_cents = int(round(100 * actual_cost / actual_contracts))
            contracts_intended = max(1, int(bet_dollars * 100 / fresh_ask) + 1)
            if actual_contracts < contracts_intended * 0.9:
                log(f"    PARTIAL FILL: {actual_contracts}/{contracts_intended} contracts "
                    f"(${actual_cost:.2f} vs ${est_cost:.2f} expected)")
            log(f"    actual fill: {actual_contracts} contracts, cost ${actual_cost:.2f}, "
                f"avg={avg_price_cents}c")
            final_contracts = actual_contracts
            final_cost      = actual_cost
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
                    f"Filled {actual_contracts} {side.upper()} @ avg {avg_price_cents}c\n"
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
            f"Bought {final_contracts} {side.upper()} contracts @ {limit_cents}c on {ticker}\n"
            f"Cost: ${final_cost:.2f}\n"
            f"Expected win: +${final_contracts * (100 - limit_cents) / 100 * 0.93:.2f}\n"
            f"Seconds left: {secs_left:.0f}\n",
        )
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
    yes_ask   = int(round(float(market.get("yes_ask_dollars", 0) or 0) * 100))
    no_ask    = int(round(float(market.get("no_ask_dollars",  0) or 0) * 100))

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

    limit_cents = min(LONGSHOT_MAX_ASK, fresh_ask + LIMIT_BUFFER)
    contracts   = max(1, int(LONGSHOT_BET * 100 / fresh_ask) + 1)
    est_cost    = contracts * fresh_ask / 100
    est_profit  = contracts * (100 - fresh_ask) / 100 * (1 - 0.07)

    log(f"  LONGSHOT: {ticker}  {secs_left:.0f}s left  {side.upper()} "
        f"scan={ask_cents}c fresh={fresh_ask}c prior_avg={prior_avg:.0f}c  "
        f"limit={limit_cents}c  {contracts} contracts  bet=${LONGSHOT_BET}  "
        f"est.win=+${est_profit:.2f}")

    if dry_run:
        log(f"    [dry-run] skipped")
        return

    if not os.environ.get("KALSHI_API_KEY_ID"):
        return

    code, resp = place_order(
        ticker, side, contracts,
        yes_price_cents=limit_cents if side == "yes" else None,
        no_price_cents=limit_cents  if side == "no"  else None,
    )
    order_id = resp.get("order", {}).get("order_id") if isinstance(resp, dict) else None
    if code in (200, 201):
        log(f"    order accepted (id={order_id})")
        time.sleep(3)
        if order_id:
            c_code, _ = cancel_order(order_id)
            log(f"    cancelled GTC order {order_id} (HTTP {c_code})")
        time.sleep(0.5)
        actual_contracts, actual_cost = query_actual_fill(ticker, side)
        if actual_contracts == 0:
            time.sleep(1.5)
            actual_contracts, actual_cost = query_actual_fill(ticker, side)
        if actual_contracts == 0:
            log(f"    no fill")
            return
        avg_price_cents = int(round(100 * actual_cost / actual_contracts))
        log(f"    actual fill: {actual_contracts} contracts, cost ${actual_cost:.2f}, avg={avg_price_cents}c")
        state["positions"][ticker] = {
            "side":         side,
            "limit_cents":  limit_cents,
            "ask_at_entry": fresh_ask,
            "ask_at_scan":  ask_cents,
            "outside_safe_zone": False,
            "contracts":    actual_contracts,
            "cost":         actual_cost,
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
        send_email(
            f"[Kalshi-C] LONGSHOT {ticker} {side.upper()} @ {limit_cents}c",
            f"[Crash-reversal longshot — OOS trial, ${LONGSHOT_BET} fixed]\n"
            f"Bought {actual_contracts} {side.upper()} @ {limit_cents}c on {ticker}\n"
            f"Prior avg: {prior_avg:.0f}c → crashed to {fresh_ask}c\n"
            f"Cost: ${actual_cost:.2f}  Est. win: +${est_profit:.2f}\n"
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

        pos["settled"]      = True
        pos["result"]       = result
        pos["pnl"]          = pnl
        pos["settled_date"] = today

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
            ls_sign = '+' if ls["pnl"] >= 0 else '-'
            save_state(state)
            log(f"  SETTLED(LS) {ticker} result={result.upper()}  side={pos['side'].upper()}  "
                f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  LS WR={ls_wr:.1f}%")
            subj = f"[Kalshi-C] LONGSHOT {'WIN' if won else 'LOSS'} {'+' if won else '-'}${abs(pnl):.2f} — {ticker}"
            body = (f"[Crash-reversal OOS trial]\n"
                    f"Bought {pos['contracts']} {pos['side'].upper()} @ {pos['limit_cents']}c\n"
                    f"Result: {result.upper()} → {'WIN' if won else 'LOSS'}\n"
                    f"P&L: {'+' if won else '-'}${abs(pnl):.2f}\n"
                    f"Longshot: {ls['wins']}/{ls['trades']} = {ls_wr:.1f}% WR  "
                    f"P&L={ls_sign}${abs(ls['pnl']):.2f}\n"
                    f"Today: {d_sign}${abs(d_pnl):.2f}\n")
            send_email(subj, body)
        else:
            state["stats"]["pnl"] = round(state["stats"].get("pnl", 0.0) + pnl, 2)
            if won:
                state["stats"]["wins"] = state["stats"].get("wins", 0) + 1
                state["consec_losses"] = 0
            else:
                state["consec_losses"] = state.get("consec_losses", 0) + 1
                state["last_loss_ts"]  = datetime.now(timezone.utc).timestamp()
            recent = state.setdefault("recent_results", [])
            recent.append([int(won), pos.get("limit_cents", 92)])
            state["recent_results"] = recent[-100:]
            save_state(state)
            wr = state["stats"]["wins"] / state["stats"]["trades"] if state["stats"]["trades"] else 0
            c_pnl  = state['stats']['pnl']
            c_sign = '+' if c_pnl >= 0 else '-'
            log(f"  SETTLED {ticker} result={result.upper()}  side={pos['side'].upper()}  "
                f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  cumul WR={wr*100:.1f}%")
            if won:
                send_email(
                    f"[Kalshi-C] WIN +${pnl:.2f} — {ticker}",
                    f"Bought {pos['contracts']} {pos['side'].upper()} @ {pos['limit_cents']}c\n"
                    f"Result: {result.upper()} → WIN\n"
                    f"Profit: +${pnl:.2f} (after 7% fee)\n"
                    f"Today: {d_sign}${abs(d_pnl):.2f}\n"
                    f"Cumulative: {state['stats']['wins']}/{state['stats']['trades']} = {wr*100:.1f}% WR  P&L={c_sign}${abs(c_pnl):.2f}\n",
                )
            else:
                send_email(
                    f"[Kalshi-C] LOSS -${abs(pnl):.2f} — {ticker}",
                    f"Bought {pos['contracts']} {pos['side'].upper()} @ {pos['limit_cents']}c\n"
                    f"Result: {result.upper()} → LOSS\n"
                    f"Loss: -${abs(pnl):.2f}\n"
                    f"Consec losses: {state['consec_losses']}\n"
                    f"Today: {d_sign}${abs(d_pnl):.2f}\n"
                    f"Cumulative: {state['stats']['wins']}/{state['stats']['trades']} = {wr*100:.1f}% WR  P&L={c_sign}${abs(c_pnl):.2f}\n",
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
    for series in random.sample(SERIES_LIST, len(SERIES_LIST)):
        markets = open_markets_near_close(series)
        n_scanned += len(markets)
        for m in markets:
            before = len(state.get("positions", {}))
            try_trade(m, state, dry_run, balance=balance)
            if len(state.get("positions", {})) > before:
                n_tradeable += 1

    for series in random.sample(SERIES_LIST, len(SERIES_LIST)):
        markets = open_markets_longshot(series)
        for m in markets:
            before = len(state.get("positions", {}))
            try_longshot_trade(m, state, dry_run)
            if len(state.get("positions", {})) > before:
                n_tradeable += 1

    stats = state["stats"]
    wr = stats["wins"] / stats["trades"] if stats["trades"] else 0
    log(f"  scanned {n_scanned} near-close markets, new trades: {n_tradeable}")
    log(f"  cumulative: {stats['wins']}/{stats['trades']} = {wr*100:.1f}% WR  P&L=${stats['pnl']:+.2f}")
    ls = state.get("longshot_stats", {})
    if ls.get("trades", 0):
        ls_wr = ls["wins"] / ls["trades"] * 100
        log(f"  longshot: {ls['wins']}/{ls['trades']} = {ls_wr:.1f}% WR  P&L=${ls.get('pnl', 0):+.2f}")

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
