#!/usr/bin/env python3
"""Late-certainty trader v5.12 — late-certainty YES entries.

STRATEGY:
  Buy YES at a 90-93 cent ask with 150-600 seconds remaining when each of
  the two preceding 1-minute YES asks was at least 75 cents. Hold through
  settlement. ET hour 13 (1pm ET) is excluded. Live series are the six 15-minute
  crypto markets; excluded candidates are shadow-logged only.

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

import argparse, base64, hashlib, json, math, os, smtplib, time, urllib.request, urllib.error, uuid
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
from email.mime.text import MIMEText
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

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
    # Explicitly EXCLUDED:
    # - KXWTI15M — PAUSED 2026-08-19, was added v5.8 on a 13-day backtest (+$1.75/trade
    #   OOS). Over its whole life it now measures -$0.33/trade on 290 trades, so the
    #   evidence that justified adding it inverted. It launched 2026-07-31, the same day
    #   as GOLD and SILVER, which are excluded on the same thin sample — trading one and
    #   not the others was an accident of timing, not a decision. Paused, not condemned:
    #   -$0.33 is ~1.1 SE from break-even and proves nothing either way. The archiver
    #   still collects it; revisit all three together at ~1,000 trades each.
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
# GOLD/SILVER added 2026-08-19 alongside WTI: all three are judged together at ~1,000
# trades, so all three need the same evidence. The archive alone is not the same
# thing — it sees every candle, while this records only what the live poller could
# actually have caught (Invariant 6).
SHADOW_SERIES   = ["KXHYPE15M", "KXBTCD", "KXETHD", "KXWTIH",
                   "KXWTI15M", "KXGOLD15M", "KXSILVER15M"]

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
# A fill a cent or two under the band is the book moving between the last look and
# the match; measured over Aug 18 those all settled +$8-13 each. Only fills deeper
# than this are a crash-through — a different bet, not a cheaper version of this one
# — and only those are worth waking someone up for.
CRASH_FILL_TOLERANCE = 3

# SHADOW ONLY — survivor re-entry (2026-08-18). A contract seen at 92-93c with 480-600s
# left and still alive at 94-96c with 150-240s left settled +3.95pp in-sample and
# +4.88pp on a time holdout. Not tradeable yet: the holdout is 41 observations with
# ~zero losses, so the rule-of-three floor is WR >= 92.7% against a 95.2c break-even —
# the pretty CI is an artifact of a near-100% win rate, not evidence. Revisit at n~500.
# Note this is the mirror image of the live gates, which buy EARLY (6-10 min out, where
# the edge is +1.27pp) and treat sub-240s entries as negative (-0.57pp).
SURVIVOR_NOW    = (94, 96)
SURVIVOR_EARLY  = (92, 93)
SURVIVOR_SECS   = (150, 240)

# SHADOW ONLY — adverse spot momentum (2026-08-20). Spot drifting TOWARD the strike in
# the 3 minutes before entry predicts losses the Kalshi ask does not price. Blocked
# bucket (m3 > +0.50) ran -$1.56/trade on n=569 over 70 days; difference vs kept trades
# CI [-3.95, -0.75], P(worse)=1.000. Pre-registered, monotone in both windows, holds
# across 5 of 6 series, both sides, all three months, and helps MORE at one tick of
# slippage — so it is not a fill-quality artifact. Logged only: no gate, no order
# effect. Confirm live before proposing a veto. research/perp_overlay/PREREG.md (H2);
# reproduce with research/perp_overlay/s1_robustness.py.
MOMENTUM_LOOKBACK   = 3    # minutes of spot move
MOMENTUM_VOL_WINDOW = 60   # trailing 1-min returns for the vol normaliser
SURVIVOR_LOOKBACK = (480, 600)

# SHADOW ONLY — poll-level gate inputs (2026-08-21). data/candles/*.csv.gz lets any
# past or future version be replayed against every archived day, but the archive is a
# 1-min sample: it sees candles, while the bot sees the ask at poll instants. 70% of
# qualifying signals last exactly one candle (invariant 6), so archive replay cannot
# tell you what a version would actually have CAPTURED live. This logs the raw gate
# inputs at each poll instead of any one version's verdict, so a config invented later
# can still be scored against it. Trades nothing, gates nothing.
GATELOG_ASK   = (88, 99)   # union of every ask band the trader has ever run
GATELOG_SECS  = (150, 900) # union of every secs band
# Hard ceiling on candle fetches per process. Capture rate is set by scan cadence, so a
# shadow that slows the loop would cost real entries to measure hypothetical ones. Each
# job is a fresh process, so this bounds the added calls per job outright. Markets seen
# earliest in the job win the budget.
#
# Raised 24 -> 96 alongside --duration 240 -> 900. The budget is per PROCESS, so a
# 3.75x longer job would otherwise exhaust it in the first quarter and log nothing for
# the rest — silently starving the very denominator the gate log exists to measure.
GATELOG_MAX_FETCHES = 96

# ── CANDLE-ALIGNED SCAN PHASE (2026-08-22) ──────────────────────────────────
# The entry criteria are defined on 1-min candle CLOSES — every secs_left in
# data/candles is a multiple of 60. The daemon used to free-run at --interval, so
# only ~1 look in 4 landed where the signal is actually defined, and the other three
# sampled the live book mid-candle where a fleeting quote can sit in [90,93]c without
# being a sustained signal.
#
# Measured over Aug 18-21: trades the model also wanted filled within 10s of a candle
# boundary 43.8% of the time; trades the model rejected did so only 20.9% of the time.
# Same defect on both sides — a phase error, not a coverage error (duty cycle is
# already 94.4%, median blind gap 13s).
#
# This changes only the PHASE of the scan grid, never its frequency: the same number
# of scans per job, repositioned so one lands just after each minute boundary.
#
# +1s, not -1s or 0: at T+1 the live ask still reflects candle T's close, and
# _prior_k_candle_asks (end_ts = now-20 = T-19) then returns candles T-1, T-2, T-3 —
# exactly the priors the model evaluates for an entry on candle T. Reading before the
# boundary would match the ask but shift the priors by one.
SCAN_PHASE_OFFSET = 1

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
# Flat principal-risk budget per order; fees are additional.
#
# $50 -> $25 on 2026-08-22. This is a survival decision, not a win-rate decision.
# Balance was $979.62, which is $329.62 above the $650 stop — 6.6 losses at $50.
# Aug 21-22 alone ran 87.23% on 141 trades for -$383.42, so two more days like that
# reach the stop and end the experiment permanently. At $25 the same headroom is 13.2
# losses.
#
# The point is that this costs almost nothing to learn from. Win rate is
# size-independent, and MIN_BOOK_DEPTH is an absolute contract count, so the bot takes
# exactly the same trades and the statistic accrues at full speed — 2,418 trades is
# still ~27 days. Only the dollar exposure halves. compute_daily_loss_limit stays at
# $300 because of the max() floor; cutting the bet does NOT retighten it this time.
#
# Restore to $50 when EITHER the reconciliation explains the capture gap, OR ~2,418
# clean trades confirm the win rate above break-even. Not on a good week.
FLAT_BET_DOLLARS = 25


def compute_bet_dollars(balance):
    return FLAT_BET_DOLLARS


def compute_daily_loss_limit(bet_dollars):
    """Trailing-24h realized-loss floor, never tighter than the validated $300.

    The floor matters: the control was validated at $300 with $75 bets (fires ~7 days
    in 68; the trades it blocks run 90.51% WR / -$1.62 per trade, worth +$802 over 68
    days). Scaling purely off bet size meant cutting the bet to $50 on 2026-08-19
    silently retightened the threshold to $200 — a level nothing has ever tested, and
    which halted the trader for most of a day. Restoring $300 returns the validated
    dollar level; the bet_dollars term still scales it upward for larger sizing.
    """
    return max(300, bet_dollars * 4)

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
# Depth required is now a MULTIPLE OF THE ORDER, not a constant. The constant 60 was
# calibrated when the bet was $75 (~81 contracts), where it meant "about three quarters
# of my order". At $25 the order is 26 contracts, so 60 silently became 2.3x — a gate
# that tightened every time the bet was cut, which is backwards: a smaller order needs
# LESS depth to fill, not more. An audit on 2026-08-22 found it rejecting a 30-contract
# BNB NO entry at 161s left that a 26-contract order would have filled outright.
#
# 1.5x covers the order with a 50% buffer for the book moving between the last-look
# read and the order landing. The floor stops it trading into a genuinely illiquid book
# however small the order gets.
#
# Note this LOOSENS the gate at $25 (39 vs 60) and TIGHTENS it at $50+ (80 vs 60).
# Both are intended: the purpose is filling the order without partials, and that scales
# with the order. Zero partial fills observed in the six orders since the $25 cut.
# How far outside the entry band a LISTING quote may sit and still earn a real quote
# refetch. Median listing-vs-market disagreement measured at 1.8c, so 3c covers the
# tail without pulling in markets that are nowhere near the band. Pre-filter only —
# the true band is enforced against the refetched ask.
LISTING_QUOTE_TOLERANCE = Decimal("3")

MIN_BOOK_DEPTH_MULTIPLE = 1.5
MIN_BOOK_DEPTH_FLOOR    = 25


def min_book_depth(bet_dollars=None, limit_cents=None):
    """Contracts of depth required at or below the limit, for THIS order's size."""
    bet = FLAT_BET_DOLLARS if bet_dollars is None else bet_dollars
    limit = Decimal(str(MAX_ASK_CENTS if limit_cents is None else limit_cents))
    contracts = contracts_for_risk(bet_dollars=bet, limit_cents=limit)
    return max(int(math.ceil(contracts * MIN_BOOK_DEPTH_MULTIPLE)), MIN_BOOK_DEPTH_FLOOR)


MIN_BOOK_DEPTH          = 60   # legacy constant; retained only for log/back-compat


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


# ── order-book depth helpers ──────────────────────────────────────────────────

def _empty_shadow_totals():
    """Cumulative counters that survive record pruning."""
    return {"signals": 0, "settled": 0, "wins": 0, "pnl": 0.0, "clusters": []}


def _book_side_levels(ticker, side):
    """Offers we would actually lift, as (cost_cents, qty), cheapest first.

    Kalshi quotes both sides as bids: a NO bid at P is a YES offer at (1-P), and a
    YES bid at P is a NO offer at (1-P). Buying YES lifts NO bids; buying NO lifts
    YES bids. Returns None on any API error so callers keep their fail-open contract.
    """
    code, r = kalshi_get(f"/markets/{ticker}/orderbook", {})
    if code != 200 or not r:
        return None
    field = "no_dollars" if side == "yes" else "yes_dollars"
    raw = (r.get("orderbook_fp") or {}).get(field, []) or []
    levels = []
    for level in raw:
        try:
            price, qty = Decimal(str(level[0])), Decimal(str(level[1]))
        except (IndexError, InvalidOperation, TypeError, ValueError):
            continue
        if not (price.is_finite() and qty.is_finite()) or qty <= 0:
            continue
        levels.append(((Decimal("1") - price) * Decimal("100"), qty))
    levels.sort(key=lambda lv: lv[0])
    return levels


def _book_last_look(ticker, side, limit_cents=None):
    """(best offer we would pay, depth at or below limit) from one book read.

    This is the guard the /markets quote cannot provide. `_fresh_ask_cents` is
    refetched *before* the candle gates, which cost 2-4 more API calls (~0.5-1.5s)
    — long enough for a market crossing its strike to reprice completely. Every deep
    sub-zone fill on Aug 18 (47c, 57c, 83c) had a quote inside the band at that
    point. The book read here is the last call before the order goes out.

    Returns (None, None) when the book is unavailable, preserving fail-open.
    """
    limit = Decimal(str(MAX_ASK_CENTS if limit_cents is None else limit_cents))
    levels = _book_side_levels(ticker, side)
    if levels is None:
        return None, None
    depth = sum((qty for offer, qty in levels if offer <= limit), Decimal("0"))
    best = levels[0][0] if levels else None
    return best, float(depth)


def _book_depth_no(ticker, limit_cents):
    """NO contracts offered at <= limit_cents, via the YES bid side.

    v5.16: promoted from research helper to the live NO order path. The YES-side
    counterpart (_book_depth_at_max_ask) reads NO bids and would measure the wrong
    side of the book for a NO entry. Returns None on any API error (fails open).
    """
    return _book_last_look(ticker, "no", limit_cents)[1]


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
    return _book_last_look(ticker, "yes")[1]


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


_SURVIVOR_SEEN = set()
_MOMENTUM_SEEN = set()
_MOMENTUM_CACHE = {}
_GATELOG_SEEN = set()


def _candle_ask_at(ticker, series, side, close_ts, lo_secs, hi_secs):
    """The side's ask from the 1-min candle sitting lo..hi seconds before close.

    Same mirroring as the prior-candle gate: a NO ask is 100 minus the YES bid.
    Returns None on any error — this feeds shadow logging only, never an order.
    """
    code, r = kalshi_get(
        f"/series/{series}/markets/{ticker}/candlesticks",
        {"start_ts": close_ts - hi_secs - 120, "end_ts": close_ts - lo_secs,
         "period_interval": 1},
    )
    if code != 200:
        return None
    best, best_gap = None, None
    mid = (lo_secs + hi_secs) / 2
    for c in r.get("candlesticks", []):
        secs = close_ts - c.get("end_period_ts", 0)
        if not (lo_secs <= secs <= hi_secs):
            continue
        try:
            if side == "yes":
                ask = price_cents(c["yes_ask"]["close_dollars"])
            else:
                yes_bid = price_cents(c["yes_bid"]["close_dollars"])
                ask = (Decimal("100") - yes_bid) if yes_bid and yes_bid > 0 else None
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if ask is None:
            continue
        gap = abs(secs - mid)
        if best_gap is None or gap < best_gap:
            best, best_gap = ask, gap
    return best


def shadow_survivor(market, series):
    """Log 94-96c contracts that were 92-93c six minutes earlier. Trades nothing.

    Deliberately runs after every order attempt in the cycle: it costs a candlestick
    call, and nothing in the shadow path may sit between a price check and an order.
    """
    ticker = market.get("ticker", "")
    secs_left = market.get("_secs_left", 0)
    if not (SURVIVOR_SECS[0] <= secs_left <= SURVIVOR_SECS[1]):
        return
    try:
        close_ts = int(datetime.fromisoformat(
            market.get("close_time", "").replace("Z", "+00:00")).timestamp())
    except (AttributeError, TypeError, ValueError):
        return
    for side in ("yes", "no"):
        key = (ticker, side)
        if key in _SURVIVOR_SEEN:
            continue
        ask = price_cents(market.get(f"{side}_ask_dollars"))
        if ask is None or not (SURVIVOR_NOW[0] <= ask <= SURVIVOR_NOW[1]):
            continue
        early = _candle_ask_at(ticker, series, side, close_ts, *SURVIVOR_LOOKBACK)
        if early is None or not (SURVIVOR_EARLY[0] <= early <= SURVIVOR_EARLY[1]):
            continue
        _SURVIVOR_SEEN.add(key)
        log(f"  [SHADOW:SURVIVOR94] {ticker}  {side.upper()}  now={ask}c  "
            f"early={early}c  {secs_left:.0f}s left")


def _spot_momentum(series):
    """(vol-normalised 3-min spot move, sigma) for a series, or None on any error.

    One Coinbase call covers both the 3-minute move and the 60-minute vol. Cached per
    wall-clock minute: a 240s job polls ~16 times but crosses only ~4 minute boundaries.
    """
    pair = COINBASE_PAIR.get(series)
    if not pair:
        return None
    minute = int(time.time()) // 60
    key = (series, minute)
    if key in _MOMENTUM_CACHE:
        return _MOMENTUM_CACHE[key]
    end   = minute * 60
    start = end - (MOMENTUM_VOL_WINDOW + 2) * 60
    url = (f"https://api.exchange.coinbase.com/products/{pair}/candles"
           f"?granularity=60&start={start}&end={end}")
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-v5-filter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    try:
        # Coinbase returns the STILL-FORMING bucket (start == end), whose close is
        # just the latest trade so far. Including it made m3 unreproducible after the
        # fact and measured a different quantity from the archive, which only ever
        # sees completed candles. Keep completed buckets only.
        px = {int(r[0]): float(r[4]) for r in data
              if int(r[0]) + 60 <= end}                # bucket start -> close
    except (IndexError, TypeError, ValueError):
        return None
    mins = sorted(px)
    rets = [math.log(px[b] / px[a]) for a, b in zip(mins, mins[1:])
            if b - a == 60 and px[a] > 0 and px[b] > 0]
    if len(rets) < 20:
        return None
    mu    = sum(rets) / len(rets)
    sigma = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    back  = mins[-1] - MOMENTUM_LOOKBACK * 60
    if sigma <= 0 or back not in px or px[back] <= 0:
        return None
    out = (math.log(px[mins[-1]] / px[back]) / (sigma * math.sqrt(MOMENTUM_LOOKBACK)),
           sigma)
    if len(_MOMENTUM_CACHE) < 64:      # short-lived process; keep the dict bounded
        _MOMENTUM_CACHE[key] = out
    return out


def shadow_momentum(market, series):
    """Log adverse spot momentum for markets sitting in the live entry band.

    Trades nothing and gates nothing. Runs after every order attempt for the same
    reason shadow_survivor does: it costs a Coinbase call, and nothing in the shadow
    path may sit between a price check and an order.
    """
    ticker    = market.get("ticker", "")
    secs_left = market.get("_secs_left", 0)
    if not (MIN_SECS_LEFT <= secs_left <= MAX_SECS_LEFT):
        return
    pending = []
    for side in ("yes", "no"):
        ask = price_cents(market.get(f"{side}_ask_dollars"))
        if (ask is not None and MIN_ASK_CENTS <= ask <= MAX_ASK_CENTS
                and (ticker, side) not in _MOMENTUM_SEEN):
            pending.append((side, ask))
    if not pending:
        return
    got = _spot_momentum(series)
    if got is None:
        return                          # retried next poll; nothing marked seen
    mom, sigma = got
    for side, ask in pending:
        _MOMENTUM_SEEN.add((ticker, side))
        adverse = -mom if side == "yes" else mom
        log(f"  [SHADOW:MOM3] {ticker}  {side.upper()}  {ask}c  {secs_left:.0f}s  "
            f"m3={adverse:+.2f}  sigma={sigma * 1e4:.2f}bp")


def shadow_gate_inputs(market, series):
    """Log the gate inputs for every market in the union band, once per (ticker, side).

    Deliberately records facts, not verdicts. A per-version verdict would freeze
    today's list of versions into the live trader and need an edit to score anything
    new; ask/secs/priors are sufficient for any gate set, including ones not yet
    invented. Slot allocation (MAX_CONCURRENT_POSITIONS) is a cluster-level constraint
    and is applied by the analyser, not here.

    Runs after every order attempt for the same reason shadow_momentum does: nothing
    in the shadow path may sit between a price check and an order.
    """
    ticker    = market.get("ticker", "")
    secs_left = market.get("_secs_left", 0)
    if not (GATELOG_SECS[0] <= secs_left <= GATELOG_SECS[1]):
        return
    for side in ("yes", "no"):
        ask = price_cents(market.get(f"{side}_ask_dollars"))
        if ask is None or not (GATELOG_ASK[0] <= ask <= GATELOG_ASK[1]):
            continue
        if (ticker, side) in _GATELOG_SEEN:
            continue
        if len(_GATELOG_SEEN) >= GATELOG_MAX_FETCHES:
            return
        priors = _prior_k_candle_asks(ticker, series, side, 3)
        if priors is None:
            continue                    # retried next poll; nothing marked seen
        _GATELOG_SEEN.add((ticker, side))
        # Depth is the one live gate scripts/backtest.py cannot model — its docstring
        # says so outright ("Not modelled: ... book-depth check"). Without it here,
        # every capture figure measured against the harness counts entries that were
        # never executable and is therefore an upper bound, not a miss rate. Logging it
        # is what turns "capture" into a number that can be acted on.
        # Fails open exactly like the live gate: None means the book read failed, which
        # is not the same as a thin book and must not be scored as one.
        best_offer, depth = _book_last_look(ticker, side)
        d = "None" if depth is None else f"{depth:.0f}"
        bo = "None" if best_offer is None else f"{best_offer}"
        log(f"  [SHADOW:GATE] {ticker} {side.upper()} ask={ask}c "
            f"secs={secs_left:.0f} p1={int(priors[0])} p2={int(priors[1])} "
            f"p3={int(priors[2])} depth={d} best={bo} min_depth={MIN_BOOK_DEPTH} "
            f"series={series}")


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
    # The quote used here comes from the /markets LISTING, which is not the same
    # number as the individual-market ask. An audit on 2026-08-22 compared the two 96
    # times: median disagreement 1.8c, band membership disagreed 7 times, and listing
    # vs book disagreed 14 times. Gating on the listing at the exact band therefore
    # made real opportunities invisible — the bot never refetched, so it never learned
    # the listing was wrong.
    #
    # The band below is a PRE-FILTER ONLY, widened by LISTING_QUOTE_TOLERANCE. Nothing
    # is loosened: `fresh_ask` is refetched immediately after and checked against the
    # true [MIN_ASK_CENTS, MAX_ASK_CENTS] band, so a market that is genuinely outside
    # still skips. The only effect is that near-band markets get a real quote instead
    # of being dismissed on a stale one.
    lo = MIN_ASK_CENTS - LISTING_QUOTE_TOLERANCE
    hi = MAX_ASK_CENTS + LISTING_QUOTE_TOLERANCE
    side, ask_cents = None, None
    if lo <= yes_ask <= hi:
        side, ask_cents = "yes", yes_ask
    elif not YES_ONLY and lo <= no_ask <= hi:
        side, ask_cents = "no",  no_ask
    # v5.16: the NO 90-91c shadow apparatus that used to live here is gone —
    # the NO side trades for real now. state["shadow_no_*"] keys are retained
    # (unused) so existing cached state stays schema-compatible.
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
    # Measure the listing-vs-fresh disagreement that motivated the tolerance, so the
    # width can be tuned on data instead of the audit's one-off sample. RECOVERED means
    # the listing alone would have discarded a genuinely in-band entry.
    _in_listing = MIN_ASK_CENTS <= ask_cents <= MAX_ASK_CENTS
    _in_fresh = MIN_ASK_CENTS <= fresh_ask <= MAX_ASK_CENTS
    if _in_listing != _in_fresh:
        log(f"  [QUOTE-DRIFT] {ticker} {side.upper()} listing={ask_cents}c "
            f"fresh={fresh_ask}c delta={float(fresh_ask - ask_cents):+.2f}c "
            f"{'RECOVERED' if _in_fresh else 'correctly-skipped'}")
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
    best_offer, depth = _book_last_look(ticker, side)
    # compute_bet_dollars is flat, so the default (FLAT_BET_DOLLARS) is the live size.
    # Taking it from the constant rather than a local keeps this correct wherever
    # try_trade is called from, including the tests.
    need_depth = min_book_depth(limit_cents=MAX_ASK_CENTS)
    # FAIL CLOSED when the book is entirely unreadable. This used to fail open, on the
    # reasoning that API errors should never block a valid entry. That reasoning was
    # overtaken by the last-look guard added below it, whose own comment explains why:
    # "a marketable order sweeps the book upward from the best offer, so a crashed book
    # is bought at crash prices no matter what limit we send". Failing open meant
    # ordering with neither depth nor last-look verified — precisely the deep sub-zone
    # fills on 2026-08-18 (47c, 57c, 83c) that motivated adding last-look at all.
    #
    # Scoped to the case production actually produces: _book_last_look returns
    # (None, None) together when the book read fails. A present best_offer means the
    # price guard DID work and only depth is unknown, where the residual risk is a
    # partial fill rather than a bad price — that case still trades, which is what the
    # existing partial-fill and cancellation tests depend on.
    #
    # Costs measurably ~zero: across the 6 orders since the $25 cut, 0 had depth=-1.
    #
    # The daemon now runs 61 scans per job, so a market skipped on one transient book
    # error is re-evaluated ~15s later. Missing one look is cheap; buying into an
    # unverified book is not.
    if depth is None and best_offer is None:
        log(f"  SKIP {ticker} — book unreadable; neither depth nor last-look could be "
            f"verified, failing closed")
        return
    # depth may still be None here when best_offer read but depth did not; that case
    # trades on, per the scoping note above.
    if depth is not None and depth < need_depth:
        log(f"  SKIP {ticker} — thin book: {depth:.0f} contracts at <={MAX_ASK_CENTS}c "
            f"(need {need_depth})")
        return

    # LAST LOOK — a buy limit is a ceiling, never a floor: a marketable order sweeps
    # the book upward from the best offer, so a crashed book is bought at crash
    # prices no matter what limit we send. The quote checked above is stale by the
    # 2-4 gate calls since, so the book read is what decides, and it is the last
    # call before place_order. Aug 18: 12 fills landed below the band, two of them
    # deep (47c and 57c) on quotes of 92.5c and 92.8c.
    if best_offer is not None and not (MIN_ASK_CENTS <= best_offer <= MAX_ASK_CENTS):
        log(f"  SKIP {ticker} — last look: best {side} offer {best_offer}c is outside "
            f"[{MIN_ASK_CENTS},{MAX_ASK_CENTS}]c while the quote still said {fresh_ask}c")
        return

    # Price off the book when we have it — that is what we actually pay. Cap at
    # MAX_ASK_CENTS so slippage can't push us above the OOS-validated range
    # (96c+ is EV-negative after fee).
    entry_ask     = best_offer if best_offer is not None else fresh_ask
    last_look_ts  = time.monotonic()
    limit_cents = min(Decimal(MAX_ASK_CENTS), entry_ask + Decimal(LIMIT_BUFFER))
    contracts   = contracts_for_risk(bet_dollars, limit_cents)
    est_cost    = float(Decimal(contracts) * entry_ask / Decimal("100"))
    max_cost    = float(Decimal(contracts) * limit_cents / Decimal("100"))
    est_profit  = float(Decimal(contracts) * (Decimal("100") - entry_ask) / Decimal("100") * Decimal("0.93"))

    log(f"  TRADE: {ticker}  {secs_left:.0f}s left  {side.upper()} "
        f"scan={ask_cents}c fresh={fresh_ask}c book={best_offer}c  limit={limit_cents}c  "
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
            # Same last look as the first attempt: a top-up sweeps the book too,
            # and the retry gates above cost another 1-2 calls of staleness.
            best_offer, retry_depth = _book_last_look(ticker, side)
            if best_offer is not None and not (MIN_ASK_CENTS <= best_offer <= MAX_ASK_CENTS):
                log(f"    TOP-UP STOP — last look: best {side} offer {best_offer}c is "
                    f"outside [{MIN_ASK_CENTS},{MAX_ASK_CENTS}]c (quote said {fresh_ask}c)")
                break
            if retry_depth is not None and retry_depth < MIN_BOOK_DEPTH:
                log(f"    TOP-UP STOP — thin book: {retry_depth:.0f} contracts "
                    f"at <={MAX_ASK_CENTS}c (need {MIN_BOOK_DEPTH})")
                break
            entry_ask = best_offer if best_offer is not None else fresh_ask
            last_look_ts = time.monotonic()
            limit_cents = min(Decimal(MAX_ASK_CENTS), entry_ask + Decimal(LIMIT_BUFFER))
            contracts = contracts_for_risk(remaining_budget, limit_cents)
            est_cost = float(Decimal(contracts) * entry_ask / Decimal("100"))
            log(f"    TOP-UP {attempt}/{ORDER_MAX_ATTEMPTS}: fresh={fresh_ask}c "
                f"book={best_offer}c remaining=${remaining_budget:.2f} "
                f"limit={limit_cents}c contracts={contracts}")

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
        last_look_ms = (time.monotonic() - last_look_ts) * 1000
        log(f"    order accepted attempt {attempt} (id={order_id}) "
            f"book_age={last_look_ms:.0f}ms")
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
                shortfall = Decimal(MIN_ASK_CENTS) - avg_price_cents
                if shortfall > Decimal(CRASH_FILL_TOLERANCE):
                    log(f"    CRASH FILL — filled at {avg_price_cents}c, "
                        f"{shortfall:.2f}c below safe zone "
                        f"[{MIN_ASK_CENTS},{MAX_ASK_CENTS}]; book said {entry_ask}c "
                        f"{last_look_ms:.0f}ms earlier.")
                    send_email(
                        f"[Kalshi-C] CRASH FILL {avg_price_cents:.2f}c — {ticker}",
                        f"Filled {actual_contracts} {side.upper()} @ avg {avg_price_cents:.2f}c\n"
                        f"Safe zone: [{MIN_ASK_CENTS}, {MAX_ASK_CENTS}]c\n"
                        f"Book best offer at last look: {entry_ask}c "
                        f"({last_look_ms:.0f}ms before the order)\n"
                        f"The book crashed inside the order's flight time. Position is "
                        f"held to settlement as usual — it is off-strategy, not "
                        f"automatically bad: Aug 18's two crash fills were -$47.48 "
                        f"and +$41.00.\n",
                    )
                else:
                    # Inside tolerance: cheaper than intended, not a crash. Logged
                    # for the record, no alert.
                    log(f"    fill {shortfall:.2f}c under the band at {avg_price_cents}c "
                        f"(tolerance {CRASH_FILL_TOLERANCE}c) — book said {entry_ask}c "
                        f"{last_look_ms:.0f}ms earlier")
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
    # Machine-readable execution record. Every field below is already computed for the
    # position dict; this only puts them where they can be harvested. State lives in the
    # Actions cache, so without this line the only way to measure fill quality is to
    # replay hundreds of run logs and parse prose.
    #
    # The open question it exists to answer: NO-side fill quality is entirely
    # unmeasured (CLAUDE.md §4), NO is ~half of volume since v5.16, and one cent moves
    # ~69% of profit. Compare avg_fill against book by side, over DISTRIBUTIONS — never
    # per-fill against a candle, which yields a +0.85c artifact from regression to the
    # mean because the 1-min reference is stale 47% of the time.
    try:
        _avg = (final_cost / final_contracts * 100.0) if final_contracts else 0.0
        log(f"    [EXEC] ticker={ticker} side={side} secs={secs_left:.0f} "
            f"scan={float(ask_cents):.2f} fresh={float(attempt_asks[0]):.2f} "
            f"book={float(entry_ask):.2f} depth={float(depth) if depth is not None else -1:.0f} "
            f"book_age_ms={last_look_ms:.0f} limit={float(max(attempt_limits)):.2f} "
            f"contracts={final_contracts:.0f} cost={final_cost:.2f} fee={final_fee:.4f} "
            f"avg_fill={_avg:.4f} attempts={len(order_ids)} "
            f"outside_band={int(bool(outside_safe_zone))}")
    except Exception as exc:            # a log line must never break a trading cycle
        log(f"    [EXEC] skipped: {exc}")
    state["positions"][ticker] = {
            "side":        side,
            "limit_cents": float(max(attempt_limits)),
            "ask_at_entry": float(attempt_asks[0]),
            "book_at_entry": float(entry_ask),
            "book_age_ms":   round(last_look_ms, 1),
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
    scanned = []
    # Discover every candidate BEFORE trading any of them, then allocate the
    # MAX_CONCURRENT_POSITIONS slots to the signals with the most time left.
    #
    # This used to iterate random.sample(SERIES_LIST) and trade each market on sight,
    # so when several markets qualified in one close cluster the two slots went to
    # whichever series the shuffle happened to visit first. scripts/backtest.py
    # allocates the same two slots by `sorted(..., key=lambda r: -r[5])[:max_conc]`,
    # so the bot and the model systematically picked DIFFERENT pairs — every mismatch
    # showing up as one missed model entry plus one entry the model never wanted.
    # Reconciliation over Aug 12-21 put the model-rejected entries at 90.32% WR
    # against 93.98% for the ones both took.
    #
    # Ordering by secs_left descending makes the live allocation identical to the
    # harness by construction, so the two can finally be compared like for like.
    # Earliest-first is also the safer order on its own terms: it leaves the most room
    # for the order to land before MIN_SECS_LEFT.
    #
    # Discovery is now a separate pass, so a market is fetched slightly before it is
    # traded. try_trade re-reads the ask immediately before ordering (_fresh_ask_cents)
    # and fails closed if it has left the band, so the extra latency cannot widen the
    # entry price.
    # Discover every series CONCURRENTLY so the decision snapshot is a snapshot.
    #
    # Sequentially this took 3.85s across 6 series (measured, run 32587469934), so the
    # first series was evaluated nearly four seconds before the last and the two slots
    # were allocated from six different moments in time. The ask can traverse the whole
    # [90,93] band in that long — invariant 6 has 70% of signals lasting a single
    # candle. Concurrent discovery collapses the skew to about one round trip.
    #
    # Reads only. Order placement below stays strictly sequential, and try_trade still
    # re-reads the ask immediately before ordering. kalshi_auth.get is stateless — it
    # signs fresh headers per call and holds no shared mutable state — so this is safe
    # to fan out. Falls back to sequential on any executor failure rather than skipping
    # a scan.
    candidates = []
    try:
        with ThreadPoolExecutor(max_workers=len(SERIES_LIST)) as ex:
            found = list(ex.map(open_markets_near_close, SERIES_LIST))
    except Exception as exc:
        log(f"  concurrent discovery failed ({exc}) — falling back to sequential")
        found = [open_markets_near_close(sr) for sr in SERIES_LIST]
    for series, markets in zip(SERIES_LIST, found):
        n_scanned += len(markets)
        for m in markets:
            candidates.append((series, m))
    # Tie-break deterministically but WITHOUT favouring any series.
    #
    # All ~6 series close simultaneously (invariant 3), so within a cluster every
    # candidate carries the same _secs_left and the sort key is a total tie. A stable
    # sort then falls back to SERIES_LIST order, which would hand both slots to BTC and
    # ETH in every cluster, forever. Measured since Aug 5 that is the wrong pair —
    # BTC -$1.099/tr and ETH -$0.551/tr against BNB +$1.062 and XRP +$1.222. The
    # ranking is only ~1.2 sigma and is probably noise, but that is precisely the
    # argument against a fixed order: concentrating every slot into two series turns a
    # diversified sample into a bet on whichever two are listed first, for no measured
    # gain (slot allocation is worth ~$1.29/day).
    #
    # Hashing (cluster, series) keeps allocation reproducible for a given cluster while
    # spreading it uniformly across series over many clusters — measured 16.2-17.1% of
    # slots per series against a 16.67% fair share. hashlib, not hash(): the builtin is
    # salted per process and would not reproduce across runs.
    def _slot_key(sm):
        series, m = sm
        tk = m.get("ticker", "")
        cluster = tk.split("-")[1] if "-" in tk else ""
        h = hashlib.md5(f"{cluster}|{series}".encode()).hexdigest()[:8]
        return (-m.get("_secs_left", 0), int(h, 16))

    candidates.sort(key=_slot_key)
    for series, m in candidates:
        scanned.append((series, m))
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

    # Survivor re-entry candidate — logged, never traded. Reuses the markets already
    # fetched above, and runs only after every order attempt is done.
    for series, m in scanned:
        try:
            shadow_survivor(m, series)
        except Exception as exc:       # a shadow log must never break a trading cycle
            log(f"  [SHADOW:SURVIVOR94] skipped {m.get('ticker','')}: {exc}")
        try:
            shadow_momentum(m, series)
        except Exception as exc:
            log(f"  [SHADOW:MOM3] skipped {m.get('ticker','')}: {exc}")
        try:
            shadow_gate_inputs(m, series)
        except Exception as exc:
            log(f"  [SHADOW:GATE] skipped {m.get('ticker','')}: {exc}")

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


def next_scan_time(interval, now=None):
    """Next instant on the candle-aligned scan grid.

    Grid is anchored to the minute so that SCAN_PHASE_OFFSET seconds after every
    candle close is always a scan slot. Requires interval to divide 60; anything else
    would drift off the boundary within a few minutes, so it falls back to a plain
    fixed delay rather than silently sampling the wrong phase.
    """
    now = time.time() if now is None else now
    if interval <= 0 or 60 % interval:
        return now + interval
    minute = math.floor(now / 60) * 60
    for k in range(0, 60, interval):
        t = minute + k + SCAN_PHASE_OFFSET
        if t > now:
            return t
    return minute + 60 + SCAN_PHASE_OFFSET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",    action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status",  action="store_true")
    ap.add_argument("--daemon",  action="store_true",
                    help="poll repeatedly instead of scanning once")
    ap.add_argument("--interval", type=int, default=20,
                    help="seconds between polls in daemon mode (default 20)")
    ap.add_argument("--duration", type=int, default=0,
                    help="stop after N seconds (0 = run forever). Used by CI so one "
                         "job performs many scans instead of one.")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(load_state(), indent=2))
        return
    if a.daemon:
        # Signal-capture fix (Aug 17). 70% of qualifying signals are in-band for a
        # single 1-min candle — the ask passes THROUGH 90-93c on its way to 100c
        # rather than resting there. One scan per CI job caught only ~27% of the
        # signals the backtest takes, and the missed ones are not worse
        # (transient 93.30% WR / +$0.58 per trade vs persistent 93.27% / +$0.77).
        # Polling many times per job converts CI startup overhead into scans.
        deadline = time.time() + a.duration if a.duration else None
        log(f"=== DAEMON — every {a.interval}s"
            + (f", stopping after {a.duration}s ===" if deadline else ", indefinitely ==="))
        cycles = aligned = 0
        while True:
            try:
                run_once(dry_run=a.dry_run)
                cycles += 1
            except KeyboardInterrupt:
                log("Daemon stopped")
                break
            except Exception as e:
                log(f"cycle error: {e}")
            # Stop before sleeping past the deadline so the job ends predictably
            # and the next dispatch is not delayed.
            nxt = next_scan_time(a.interval)
            if deadline and nxt >= deadline:
                log(f"=== DAEMON done — {cycles} scans in {a.duration}s "
                    f"({aligned} candle-aligned) ===")
                break
            if int(nxt) % 60 == SCAN_PHASE_OFFSET:
                aligned += 1
            time.sleep(max(0.0, nxt - time.time()))
        return
    try:
        run_once(dry_run=a.dry_run)
    except Exception as e:
        log(f"FATAL: {e}")
        raise


if __name__ == "__main__":
    main()
