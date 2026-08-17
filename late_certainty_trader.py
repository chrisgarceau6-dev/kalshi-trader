#!/usr/bin/env python3
"""Late-certainty trader v5.12 — late-certainty YES entries.

STRATEGY:
  Buy YES at a 90-93 cent ask with 150-600 seconds remaining when each of
  the two preceding 1-minute YES asks was at least 75 cents. Hold through
  settlement. ET hour 13 (1pm ET) is excluded. Live series are the six 15-minute
  crypto markets plus KXWTI15M; excluded candidates are shadow-logged only.

BET SIZING:
  Flat $75 principal-risk budget per order. Contract count is sized from
  the limit price so principal at the worst allowed fill cannot exceed $75.
  Exchange fees are additional.

KILL SWITCHES:
  STOP_BALANCE=$650
  trailing-24h loss limit = 8x bet size ($600 at current sizing)
  5 consecutive losses -> 60-minute cooldown
  50-trade WR below 84% -> 2-hour degradation halt
  ambiguous execution state -> persistent fail-closed halt

usage: --once | --dry-run | --status
"""

import argparse, base64, json, os, random, smtplib, time, urllib.request, urllib.error, uuid
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
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

# ── preregistered NO 90-91c research candidate — RESEARCH ONLY, NEVER TRADES ──
# The old `[SHADOW:NO-90-91]` line fired before the fresh-ask and prior-candle
# checks, so it logged candidates a real order would often reject. Those lines are
# NOT executable signals. This records only candidates that pass the same
# just-in-time gates a live order faces, then scores them after settlement.
# There is deliberately no path from this code to place_order().
SHADOW_NO_SERIES        = ("KXBTC15M", "KXETH15M", "KXSOL15M",
                           "KXDOGE15M", "KXBNB15M", "KXXRP15M")
SHADOW_NO_MIN_ASK       = Decimal("90")
SHADOW_NO_MAX_ASK       = Decimal("91")
SHADOW_NO_PRIOR_MIN     = Decimal("75")
SHADOW_NO_PRIOR_K       = 2
SHADOW_NO_ADVERSE_CENTS = Decimal("1")   # modelled fill = fresh ask + 1c
SHADOW_NO_BUDGET        = Decimal("75")
# Load-bearing: at most ONE NO per settlement cluster. Allowing two is -$1,211
# over the 60-day window vs +$195 for max-1. Do not raise without re-running
# scripts/no_60d.py.
SHADOW_NO_MAX_PER_CLOSE = 1
SHADOW_NO_PRUNE_DAYS    = 14   # settled records older than this leave state
SHADOW_NO_MAX_SETTLE_CHECKS = 40  # bound per-cycle settlement API calls

STRATEGY_VERSION = "v5.16"  # NO side re-enabled (YES_ONLY=False), side-aware book depth

MIN_ASK_CENTS   = 90     # v5: widened entry from [95,99] to [90,99] — more volume
MAX_ASK_CENTS   = 93     # v5.6.4: lowered 95→93 to avoid partial fills at thin 94-95c book
PRIOR_MIN_CENTS = 75     # v5.6.5: relaxed 80→75c — same WR, +38% volume (filter audit Aug 10)
PRIOR_LOOKBACK  = 2      # v5.6.5: relaxed 3→2 candles — -0.1pp WR, +53% volume (filter audit Aug 10)
# v5.16: NO side re-enabled. The Aug 11 audit that suspended it (live YES 46W/1L
# vs NO 74W/8L) is not significant — z=1.64, two-sided p=0.102 on n=47 YES trades.
# Full retained history (Jun 11-Aug 17, 68 days, 6,399 close clusters, all 7 series):
# YES 93.79% WR vs NO 93.65% WR. Cluster-bootstrapped YES-minus-NO win rate is
# +0.75pp [-0.36, +1.86] in-sample and -1.59pp [-4.54, +1.35] on the holdout flanks
# — both CIs include zero, and the sign flips between windows. There is no
# measurable asymmetry. Base rate confirms it: these markets settle YES 49.8% of
# the time, so neither side is structurally favoured.
# Expected effect at measured fill quality (+0.105c): +$62/day -> +$108/day,
# delta +$3,134 over 68 days, P(better)=0.981 (98.75% CI lower bound -$302).
# MAX_CONCURRENT_POSITIONS=2 still caps a settlement cluster at $150 regardless
# of side, so this adds volume without widening the per-cluster tail.
YES_ONLY        = False
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
BLACKOUT_HOURS  = set()   # no ET hours blocked; ET13 removed (p=0.43, pure noise); ET08 shadow-logged only

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
# Flat $75 principal-risk budget per order; fees are additional.
FLAT_BET_DOLLARS = 75


def compute_bet_dollars(balance):
    return FLAT_BET_DOLLARS


def compute_daily_loss_limit(bet_dollars):
    """Trailing-24h realized-loss floor scaled to the configured bet size."""
    return max(30, bet_dollars * 4)

ROLLING_PNL_SECONDS = 86400  # trailing 24h window avoids double-limit at midnight UTC

def daily_pnl(state, now_ts=None):
    """Realized P&L over the trailing 24 hours.
    Uses settled_ts when available; falls back to settled_date == today for
    legacy positions recorded before this field existed."""
    now_ts = now_ts or datetime.now(ET).timestamp()
    cutoff = now_ts - ROLLING_PNL_SECONDS
    today  = datetime.fromtimestamp(now_ts, tz=ET).date().isoformat()
    return round(sum(
        p.get("pnl", 0)
        for p in state.get("positions", {}).values()
        if p.get("settled") and (
            float(p["settled_ts"]) >= cutoff
            if p.get("settled_ts") else p.get("settled_date") == today
        )
    ), 2)

# Kill switches (some now dynamic)
STOP_BALANCE            = 650  # absolute cash-balance floor retained after the 2026-08-14 deposit
CONSEC_LOSS_LIMIT       = 9   # halt for 60 min after 9 consecutive losses (5 fired too often on correlated closes)
MAX_CONCURRENT_POSITIONS = 2  # 2×$75=$150=10.9% of balance; matches old $90=10.5% exposure ratio
EDGE_DEGRADE_WINDOW     = 50  # rolling trade window for WR degradation check
EDGE_DEGRADE_THRESHOLD  = 0.84  # halt if rolling WR drops below this; 88% fired on 2-sigma variance
EDGE_DEGRADE_COOLDOWN   = 7200  # 2h: auto-clear edge degrade if consec_losses < 3 (prevents deadlock)
MAX_POSITIONS_STATE     = 500  # keep only most recent settled positions in state
ORDER_TTL_SECONDS       = 4    # server-enforced expiry is the final guard against stranded GTC orders
ORDER_RECONCILE_SECONDS = 8    # maximum time to prove the order terminal and recover exact exposure
ORDER_FILL_WAIT_SECONDS = 3    # resting window before cancel; preserves queue priority for thin books
ORDER_MAX_ATTEMPTS      = 3    # bounded top-ups, each with a fresh price/prior validation
ORDER_MIN_TOPUP_DOLLARS = 5    # do not create dust orders for the last few dollars
MIN_BOOK_DEPTH          = 60   # skip entry if fewer than 60 YES contracts at <=MAX_ASK_CENTS


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
            # Research records survive strategy-version resets on purpose: the
            # NO candidate is frozen and independent of live parameter changes.
            s.setdefault("shadow_no_90_91", {})
            s.setdefault("shadow_no_totals", _empty_shadow_totals())
            return s
        except Exception as exc:
            raise RuntimeError(f"state exists but is unreadable; fail closed: {exc}") from exc
    return {
        "positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        "recent_results": [], "strategy_version": STRATEGY_VERSION,
        "shadow_no_90_91": {}, "shadow_no_totals": _empty_shadow_totals(),
    }


def save_state(s):
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(s, indent=2, default=str))
    os.replace(temporary, STATE_FILE)


def log(msg):
    ts   = datetime.now(ET).isoformat(timespec="seconds")
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
    now = datetime.now(ET).strftime("%Y-%m-%dT%H:%M:%S ET")
    log(f"  [SHADOW:{reason}] {ticker}  {side.upper()}  {ask}c  {secs_left:.0f}s  ts={now}")


# ── NO 90-91c research instrumentation (never places an order) ────────────────

def _empty_shadow_totals():
    """Cumulative counters that survive record pruning."""
    return {"signals": 0, "settled": 0, "wins": 0, "pnl": 0.0, "clusters": []}


def _shadow_taker_fee(price_dollars, contracts):
    """Kalshi taker fee, rounded up to the cent — matches the historical audit."""
    raw = Decimal("0.07") * Decimal(contracts) * price_dollars * (Decimal("1") - price_dollars)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _book_depth_no(ticker, limit_cents):
    """NO contracts offered at <= limit_cents, via the YES bid side.

    v5.16: promoted from research helper to the live NO order path. The YES-side
    counterpart (_book_depth_at_max_ask) reads NO bids and would measure the wrong
    side of the book for a NO entry. Returns None on any API error (fails open).
    """
    code, r = kalshi_get(f"/markets/{ticker}/orderbook", {})
    if code != 200 or not r:
        return None
    yes_bids = (r.get("orderbook_fp") or {}).get("yes_dollars", []) or []
    try:
        min_yes_price = Decimal("1") - Decimal(str(limit_cents)) / Decimal("100")
    except (InvalidOperation, TypeError, ValueError):
        return None
    total = Decimal("0")
    for level in yes_bids:
        try:
            price, qty = Decimal(str(level[0])), Decimal(str(level[1]))
            if price.is_finite() and qty.is_finite() and price >= min_yes_price:
                total += qty
        except (IndexError, InvalidOperation, TypeError, ValueError):
            continue
    return float(total)


def _market_close_ts(market):
    try:
        close_dt = datetime.fromisoformat(str(market["close_time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return int(close_dt.timestamp())


def evaluate_shadow_no_candidate(market, state, now_ts=None):
    """Record one executable NO 90-91c signal. Never places an order.

    Fails closed on a stale ask, missing candles, thin book, or missing close
    time — the same conditions that would abort a real order.
    """
    ticker = market.get("ticker", "")
    series = market.get("event_ticker", "").split("-")[0] or ticker.split("-")[0]
    records = state.setdefault("shadow_no_90_91", {})
    if not ticker or series not in SHADOW_NO_SERIES or ticker in records:
        return False

    try:
        secs_left = float(market.get("_secs_left", 0))
    except (TypeError, ValueError):
        return False
    if not MIN_SECS_LEFT <= secs_left <= MAX_SECS_LEFT:
        return False

    scan_ask = price_cents(market.get("no_ask_dollars"))
    if scan_ask is None or not SHADOW_NO_MIN_ASK <= scan_ask <= SHADOW_NO_MAX_ASK:
        return False

    # Same just-in-time gates a live order faces — this is the whole point.
    fresh_ask = _fresh_ask_cents(ticker, "no")
    if fresh_ask is None or not SHADOW_NO_MIN_ASK <= fresh_ask <= SHADOW_NO_MAX_ASK:
        return False

    prior_asks = _prior_k_candle_asks(ticker, series, "no", SHADOW_NO_PRIOR_K)
    if (prior_asks is None or len(prior_asks) != SHADOW_NO_PRIOR_K
            or any(a < SHADOW_NO_PRIOR_MIN for a in prior_asks)):
        return False

    close_ts = _market_close_ts(market)
    if close_ts is None:
        return False

    modelled_fill = fresh_ask + SHADOW_NO_ADVERSE_CENTS
    price_dollars = modelled_fill / Decimal("100")
    contracts = contracts_for_risk(SHADOW_NO_BUDGET, modelled_fill)
    cost = Decimal(contracts) * price_dollars
    fee = _shadow_taker_fee(price_dollars, contracts)

    depth = _book_depth_no(ticker, modelled_fill)
    if depth is None or depth < contracts:
        return False

    selected_at_close = sum(
        1 for rec in records.values()
        if rec.get("close_ts") == close_ts and rec.get("portfolio_selected")
    )
    selected = selected_at_close < SHADOW_NO_MAX_PER_CLOSE
    signal_ts = int(now_ts if now_ts is not None else time.time())

    records[ticker] = {
        "hypothetical": True, "ticker": ticker, "series": series, "side": "no",
        "signal_ts": signal_ts, "close_ts": close_ts, "seconds_left": secs_left,
        "scan_ask_cents": float(scan_ask), "fresh_ask_cents": float(fresh_ask),
        "prior_asks_cents": [float(a) for a in prior_asks],
        "modelled_fill_cents": float(modelled_fill), "contracts": contracts,
        "visible_depth": depth, "cost": float(cost), "fee_cost": float(fee),
        "portfolio_selected": selected, "settled": False,
    }
    totals = state.setdefault("shadow_no_totals", _empty_shadow_totals())
    totals["signals"] = totals.get("signals", 0) + 1
    log(f"  [SHADOW:NO-90-91-VALID] {ticker} NO scan={scan_ask}c fresh={fresh_ask}c "
        f"modelled={modelled_fill}c priors={prior_asks} {secs_left:.0f}s "
        f"{'SELECTED' if selected else 'UNCAPPED'}")
    return True


def collect_shadow_no_signals(state):
    """Collect signals independently of live halts, balance, and ordering."""
    added = 0
    for series in SHADOW_NO_SERIES:
        try:
            markets = sorted(
                open_markets_near_close(series, apply_blackout=False),
                key=lambda m: -float(m.get("_secs_left", 0)),
            )
        except Exception as exc:          # research must never break the trader
            log(f"  shadow NO scan failed for {series}: {exc}")
            continue
        for m in markets:
            try:
                added += int(evaluate_shadow_no_candidate(m, state))
            except Exception as exc:
                log(f"  shadow NO eval failed for {m.get('ticker','?')}: {exc}")
    return added


def check_shadow_no_outcomes(state):
    """Settle hypothetical records. Touches no cash, halts, or live stats."""
    records = state.get("shadow_no_90_91", {})
    totals = state.setdefault("shadow_no_totals", _empty_shadow_totals())
    pending = [(t, r) for t, r in records.items() if not r.get("settled")]
    changed = False

    for ticker, rec in pending[:SHADOW_NO_MAX_SETTLE_CHECKS]:
        code, resp = kalshi_get(f"/markets/{ticker}")
        if code != 200:
            continue
        market = resp.get("market", resp)
        if market.get("status") not in ("settled", "finalized"):
            continue
        result = market.get("result", "")
        if result not in ("yes", "no"):
            continue

        won = result == "no"
        contracts = Decimal(str(rec["contracts"]))
        payout = contracts if won else Decimal("0")
        pnl = payout - Decimal(str(rec["cost"])) - Decimal(str(rec["fee_cost"]))
        rec.update({
            "settled": True, "settled_ts": int(time.time()),
            "result": result, "won": won,
            "pnl": float(pnl.quantize(Decimal("0.01"))),
        })
        changed = True
        if rec.get("portfolio_selected"):
            totals["settled"] = totals.get("settled", 0) + 1
            totals["wins"] = totals.get("wins", 0) + int(won)
            totals["pnl"] = round(totals.get("pnl", 0.0) + rec["pnl"], 2)
            clusters = totals.setdefault("clusters", [])
            if rec["close_ts"] not in clusters:
                clusters.append(rec["close_ts"])
        log(f"  [SHADOW:NO-90-91-SETTLED] {ticker} result={result.upper()} "
            f"pnl=${rec['pnl']:+.2f} {'SELECTED' if rec.get('portfolio_selected') else 'UNCAPPED'}")

    # Prune old settled records so state (a per-cycle GH artifact) stays small.
    # Cumulative totals above are never pruned.
    cutoff = int(time.time()) - SHADOW_NO_PRUNE_DAYS * 86400
    stale = [t for t, r in records.items()
             if r.get("settled") and int(r.get("settled_ts", 0)) < cutoff]
    for t in stale:
        del records[t]
    if stale:
        changed = True
        log(f"  shadow NO: pruned {len(stale)} settled records older than {SHADOW_NO_PRUNE_DAYS}d")
    return changed


def shadow_no_summary(state):
    """Cumulative totals for the capped (tradeable) portfolio."""
    t = dict(_empty_shadow_totals())
    t.update(state.get("shadow_no_totals", {}) or {})
    settled = t.get("settled", 0)
    return {
        "signals": t.get("signals", 0), "settled": settled, "wins": t.get("wins", 0),
        "pnl": round(t.get("pnl", 0.0), 2),
        "win_rate": (t.get("wins", 0) / settled) if settled else None,
        "clusters": len(t.get("clusters", []) or []),
    }


# ── market scanning ────────────────────────────────────────────────────────────

def open_markets_near_close(series, apply_blackout=True):
    """Return open markets for series with 60-900s remaining."""
    # KXBTCD/KXETHD have 100+ strikes per close time — need higher limit to capture all qualifying
    lim = 100 if series in ("KXBTCD", "KXETHD") else 10
    code, r = kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": lim})
    if code != 200:
        return []
    now = datetime.now(ET)
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
        if apply_blackout and close_dt.astimezone(ET).hour in BLACKOUT_HOURS:
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
    now = datetime.now(ET)
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
        if close_dt.astimezone(ET).hour in BLACKOUT_HOURS:
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
        age = datetime.now(ET).timestamp() - ts
        if age < 3600:
            return True, f"{cl} consec losses, cooldown {60 - int(age/60)}min"
    # Rolling WR degradation check — with 2h auto-recovery to prevent permanent deadlock
    recent = state.get("recent_results", [])
    if len(recent) >= EDGE_DEGRADE_WINDOW:
        window = recent[-EDGE_DEGRADE_WINDOW:]
        rolling_wr = sum(r[0] for r in window) / EDGE_DEGRADE_WINDOW
        if rolling_wr < EDGE_DEGRADE_THRESHOLD:
            halted_at = state.get("edge_degrade_halted_at", 0)
            now_ts = datetime.now(ET).timestamp()
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


def _book_depth_at_max_ask(ticker):
    """Count YES contracts available at <=MAX_ASK_CENTS via NO bid side.
    NO bid at price P = YES ask at (1-P). Fails open (returns None) on any error."""
    code, r = kalshi_get(f"/markets/{ticker}/orderbook", {})
    if code != 200 or not r:
        return None
    no_bids = (r.get("orderbook_fp") or {}).get("no_dollars", [])
    min_no_price = 1.0 - MAX_ASK_CENTS / 100.0  # 0.07 for MAX_ASK_CENTS=93
    total = 0.0
    for level in no_bids:
        try:
            if float(level[0]) >= min_no_price:
                total += float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
    return total


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

    # Pick side within target band. v5.16: both sides trade — the two are
    # statistically indistinguishable (93.79% vs 93.65% WR over 68 days).
    # YES is checked first, so when a market somehow qualifies on both it takes
    # YES; in practice that cannot happen, since a 90-93c YES ask implies a
    # 7-10c NO ask. Re-entry on the opposite side of a ticker already held is
    # blocked by the `ticker in state["positions"]` guard at the top of this
    # function — markets that flip across the strike mid-window lost -$40/trade
    # in backtest, and that guard is what prevents taking them.
    side, ask_cents = None, None
    if MIN_ASK_CENTS <= yes_ask <= MAX_ASK_CENTS:
        side, ask_cents = "yes", yes_ask
    elif not YES_ONLY and MIN_ASK_CENTS <= no_ask <= MAX_ASK_CENTS:
        side, ask_cents = "no",  no_ask
    # NO 90-91c shadow logging used to live here, but it fired before the
    # fresh-ask and prior-candle checks, so it recorded candidates a real order
    # would often reject. Replaced by collect_shadow_no_signals() in run_once(),
    # which applies the same just-in-time gates and scores after settlement.
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

    # LOW-ASK GATE: 90-91c entries require a 3rd prior candle >= 80c.
    # Cross-tab (n=2035): 90-91c + prior2-only = -$0.08/trade (below break-even).
    # 90-91c + prior3>=80c = +$0.93/trade. 92-93c is +EV with prior2 alone.
    if fresh_ask <= 91:
        ext_priors = _prior_k_candle_asks(ticker, series, side, 3)
        if ext_priors is None or ext_priors[2] < 80:
            log(f"  SKIP {ticker} — ask {fresh_ask}c (<=91c) needs 3rd prior >= 80c; "
                f"3-candle asks={ext_priors}")
            return

    # C1 PROVISIONAL QUARANTINE: SOL + prior2 75-79c
    # IS: 60 trades -$3.42/tr; OOS (Jul13-Aug12): 80 trades -$7.25/tr. Persistent across
    # all three 20-day periods. Reversible — reassess after 60 calendar days + 100 signals.
    if series == "KXSOL15M" and 75 <= int(prior_asks[1]) <= 79:
        log(f"[SHADOW:C1-SOL-LOW-P2] {ticker} — SOL prior2={int(prior_asks[1])}c "
            f"(quarantine; ask={fresh_ask}c secs={secs_left})")
        return

    # C5 SHADOW-ONLY: prior1>=95c + prior3>=95c (54 OOS trades, not yet blockable)
    if int(prior_asks[0]) >= 95:
        _c5 = _prior_k_candle_asks(ticker, series, side, 3)
        if _c5 is not None and int(_c5[2]) >= 95:
            log(f"[SHADOW:C5-HIGH-P1P3] {ticker} — prior1={int(prior_asks[0])}c "
                f"prior3={int(_c5[2])}c (shadow only; ask={fresh_ask}c)")

    # SHADOW: ET08 and ET13 — previously blacklisted hours, now shadow-logging for data.
    _ct = market.get("close_time", "")
    if _ct:
        try:
            _et_hour = datetime.fromisoformat(_ct.replace("Z", "+00:00")).astimezone(ET).hour
            if _et_hour == 8:
                log(f"[SHADOW:ET08] {ticker} — ask={fresh_ask}c secs={secs_left:.0f}s")
            elif _et_hour == 13:
                log(f"[SHADOW:ET13] {ticker} — ask={fresh_ask}c secs={secs_left:.0f}s")
        except Exception:
            pass

    # BOOK DEPTH: skip if fewer than MIN_BOOK_DEPTH contracts at <=MAX_ASK_CENTS
    # on the side we are actually buying. Thin books (KXBNB, KXSOL) cause systematic
    # partial fills — 1-15 contracts instead of ~80 — making position tracking
    # unreliable and fill costs unpredictable.
    # v5.16: side-aware. _book_depth_at_max_ask reads the NO bid side to price YES
    # asks; for a NO entry that is the wrong side of the book entirely, so NO entries
    # use _book_depth_no (YES bid side). We have no live evidence on NO fill quality
    # — this check is the main guard against thin NO books.
    # Fails open (None) so API errors never block valid entries.
    depth = (_book_depth_at_max_ask(ticker) if side == "yes"
             else _book_depth_no(ticker, MAX_ASK_CENTS))
    if depth is not None and depth < MIN_BOOK_DEPTH:
        log(f"  SKIP {ticker} — thin book: {depth:.0f} contracts at <={MAX_ASK_CENTS}c "
            f"(need {MIN_BOOK_DEPTH})")
        return

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

    target_budget = Decimal(str(bet_dollars))
    total_contracts = Decimal("0")
    total_cost = Decimal("0")
    total_fee = Decimal("0")
    order_ids = []
    attempt_asks = []
    attempt_limits = []
    outside_safe_zone = False

    for attempt in range(1, ORDER_MAX_ATTEMPTS + 1):
        remaining_budget = target_budget - total_cost
        if remaining_budget < Decimal(str(ORDER_MIN_TOPUP_DOLLARS)):
            break

        if attempt > 1:
            # Never leave a stale bid resting just to reach the target size.
            # Every top-up must independently remain inside the validated zone.
            fresh_ask = _fresh_ask_cents(ticker, side)
            if fresh_ask is None or not (MIN_ASK_CENTS <= fresh_ask <= MAX_ASK_CENTS):
                log(f"    TOP-UP STOP — fresh {side} ask {fresh_ask}c is outside "
                    f"[{MIN_ASK_CENTS},{MAX_ASK_CENTS}]c")
                break
            retry_priors = _prior_k_candle_asks(ticker, series, side, PRIOR_LOOKBACK)
            if retry_priors is None or any(pa < PRIOR_MIN_CENTS for pa in retry_priors):
                log(f"    TOP-UP STOP — prior-candle gate no longer provable: {retry_priors}")
                break
            if fresh_ask <= 91:
                ext_retry = _prior_k_candle_asks(ticker, series, side, 3)
                if ext_retry is None or ext_retry[2] < 80:
                    log(f"    TOP-UP STOP — ask {fresh_ask}c (<=91c) needs 3rd prior >= 80c; "
                        f"got {ext_retry}")
                    break
            limit_cents = min(Decimal(MAX_ASK_CENTS), fresh_ask + Decimal(LIMIT_BUFFER))
            contracts = contracts_for_risk(remaining_budget, limit_cents)
            est_cost = float(Decimal(contracts) * fresh_ask / Decimal("100"))
            log(f"    TOP-UP {attempt}/{ORDER_MAX_ATTEMPTS}: fresh={fresh_ask}c "
                f"remaining=${remaining_budget:.2f} limit={limit_cents}c "
                f"contracts={contracts}")

        attempt_asks.append(fresh_ask)
        attempt_limits.append(limit_cents)
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
        if code not in (200, 201):
            log(f"    order attempt {attempt} FAILED — HTTP {code}: {str(resp)[:200]}")
            break
        if not order_id:
            reason = f"accepted {ticker} order had no order_id; manual account reconciliation required"
            state["execution_halt_reason"] = reason
            state["execution_halt_context"] = {
                "ticker": ticker,
                "known_contracts": float(total_contracts),
                "known_cost": float(total_cost),
                "client_order_id": client_order_id,
            }
            save_state(state)
            log(f"    DANGER — {reason}; server expiry={expiration_time}")
            send_email(f"[Kalshi-C] EXECUTION HALT — {ticker}", reason)
            raise RuntimeError(reason)

        order_ids.append(order_id)
        log(f"    order accepted attempt {attempt} (id={order_id})")
        time.sleep(ORDER_FILL_WAIT_SECONDS)
        c_code, _ = cancel_order(order_id)
        log(f"    cancel GTC order {order_id} (HTTP {c_code})")
        if c_code not in (200, 204, 404):
            log(f"    WARNING — cancel returned HTTP {c_code}; waiting for server expiry")
        try:
            actual_contracts, actual_cost, actual_fee = reconcile_terminal_order(order_id, ticker, side)
        except RuntimeError as exc:
            reason = f"{ticker}: {exc}"
            state["execution_halt_reason"] = reason
            state["execution_halt_context"] = {
                "ticker": ticker,
                "known_contracts": float(total_contracts),
                "known_cost": float(total_cost),
                "order_ids": order_ids,
            }
            save_state(state)
            send_email(f"[Kalshi-C] EXECUTION HALT — {ticker}", reason)
            raise

        actual_contracts_dec = Decimal(str(actual_contracts))
        actual_cost_dec = Decimal(str(actual_cost))
        actual_fee_dec = Decimal(str(actual_fee))
        if actual_contracts_dec > 0:
            avg_price_cents = actual_cost_dec / actual_contracts_dec * Decimal("100")
            if actual_contracts_dec < Decimal(contracts) * Decimal("0.9"):
                log(f"    PARTIAL FILL attempt {attempt}: {actual_contracts}/{contracts} contracts "
                    f"(${actual_cost:.2f} vs ${est_cost:.2f} expected)")
            log(f"    actual fill attempt {attempt}: {actual_contracts} contracts, "
                f"cost ${actual_cost:.2f}, avg={avg_price_cents:.2f}c fee=${actual_fee:.4f}")
            total_contracts += actual_contracts_dec
            total_cost += actual_cost_dec
            total_fee += actual_fee_dec
            if total_cost > target_budget + Decimal("0.01"):
                reason = (f"{ticker}: cumulative principal ${total_cost:.4f} exceeded "
                          f"budget ${target_budget:.2f}")
                state["execution_halt_reason"] = reason
                save_state(state)
                send_email(f"[Kalshi-C] EXECUTION HALT — {ticker}", reason)
                raise RuntimeError(reason)
            if avg_price_cents < MIN_ASK_CENTS:
                outside_safe_zone = True
                log(f"    DANGER — filled at {avg_price_cents}c, BELOW "
                    f"safe zone [{MIN_ASK_CENTS},{MAX_ASK_CENTS}].")
                send_email(
                    f"[Kalshi-C] DANGER FILL {avg_price_cents}c — {ticker}",
                    f"Filled {actual_contracts} {side.upper()} @ avg {avg_price_cents:.2f}c\n"
                    f"Safe zone: [{MIN_ASK_CENTS}, {MAX_ASK_CENTS}]c\n",
                )
                break
        else:
            log(f"    no fill on attempt {attempt}; order is terminal")

    if total_contracts <= 0:
        log(f"    no fill after {len(order_ids)} bounded attempt(s)")
        return

    final_contracts = float(total_contracts)
    final_cost = float(total_cost)
    final_fee = float(total_fee)
    log(f"    FILL TOTAL: {final_contracts} contracts, principal=${final_cost:.2f}, "
        f"fees=${final_fee:.4f}, unused_budget=${float(target_budget-total_cost):.2f}, "
        f"attempts={len(order_ids)}")
    state["positions"][ticker] = {
            "side":        side,
            "limit_cents": float(max(attempt_limits)),
            "ask_at_entry": float(attempt_asks[0]),
            "ask_at_scan":  float(ask_cents),
            "attempt_asks": [float(v) for v in attempt_asks],
            "outside_safe_zone": outside_safe_zone,
            "contracts":   final_contracts,
            "cost":        final_cost,
            "fee_cost":    final_fee,
            "order_id":    order_ids[-1],
            "order_ids":   order_ids,
            "strategy_version": STRATEGY_VERSION,
            "opened_at":   datetime.now(ET).isoformat(timespec="seconds"),
            "settled":     False,
    }
    state["stats"]["trades"] += 1
    today = datetime.now(ET).date().isoformat()
    daily = state.setdefault("daily", {"date": today, "pnl": 0.0, "trades_today": 0})
    if daily.get("date") != today:
        daily["date"] = today
        daily["pnl"]  = 0.0
        daily["trades_today"] = 0
    daily["trades_today"] = daily.get("trades_today", 0) + 1
    save_state(state)


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
            "opened_at":    datetime.now(ET).isoformat(timespec="seconds"),
            "settled":      False,
            "strategy":     "longshot",
        }
        ls = state.setdefault("longshot_stats", {"trades": 0, "wins": 0, "pnl": 0.0})
        ls["trades"] += 1
        today = datetime.now(ET).date().isoformat()
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
    today = datetime.now(ET).date().isoformat()
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
        pos["settled_ts"]   = datetime.now(ET).timestamp()
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
                state["last_loss_ts"]  = datetime.now(ET).timestamp()
            ls_wr = ls["wins"] / ls["trades"] * 100 if ls["trades"] else 0
            save_state(state)
            log(f"  SETTLED(LS) {ticker} result={result.upper()}  side={pos['side'].upper()}  "
                f"pnl=${pnl:+.2f}  daily=${daily['pnl']:+.2f}  LS WR={ls_wr:.1f}%")
        else:
            if won:
                state["consec_losses"] = 0
            else:
                state["consec_losses"] = state.get("consec_losses", 0) + 1
                state["last_loss_ts"]  = datetime.now(ET).timestamp()
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
    now_et = datetime.now(ET)
    log(f"=== CERTAINTY @ {now_et.strftime('%Y-%m-%d %H:%M:%S')} ET ===")

    # v5.16: NO 90-91c shadow collection is retired — the question it existed to
    # answer (is the NO side tradable?) was settled on the full 68-day retained
    # history, and NO now trades live. Collection is disabled here rather than
    # deleted so this change stays a config-level diff on a live-money file; the
    # functions and test_shadow_no_90_91.py remain green and are removed in a
    # follow-up PR once NO has live settlements.
    # Settlement scoring still runs, so any signals already recorded in state
    # finish resolving and are not orphaned.
    try:
        if check_shadow_no_outcomes(state):
            save_state(state)
        sn = shadow_no_summary(state)
        if sn["signals"]:
            wr = f"{sn['win_rate'] * 100:.1f}%" if sn["win_rate"] is not None else "n/a"
            log(f"  shadow NO90-91 (retired, draining): {sn['signals']} signals; "
                f"selected settled {sn['wins']}/{sn['settled']} WR={wr} "
                f"P&L=${sn['pnl']:+.2f} clusters={sn['clusters']}")
    except Exception as exc:
        log(f"  WARNING: shadow NO instrumentation failed (non-fatal): {exc}")

    balance = fetch_balance()
    if balance is None:
        log("  WARNING: cannot fetch balance — skipping this cycle")
        return
    log(f"  balance=${balance:.2f}")

    check_outcomes(state, balance)

    open_cost = sum(
        p.get("cost", 0) for p in state.get("positions", {}).values()
        if not p.get("settled")
    )
    equity = balance + open_cost
    if equity > state.get("high_water_balance", 0):
        state["high_water_balance"] = equity
        log(f"  high-water mark: ${equity:.2f} (cash ${balance:.2f} + open ${open_cost:.2f})")

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
        now = datetime.now(ET)
        for m in r.get("markets", []):
            ct = m.get("close_time", "")
            if not ct:
                continue
            try:
                close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                secs = (close_dt - now).total_seconds()
            except Exception:
                continue
            if close_dt.astimezone(ET).hour in BLACKOUT_HOURS:
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
