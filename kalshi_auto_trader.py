#!/usr/bin/env python3
"""Kalshi auto-executor for Polymarket copy-trade signals.

When copy_monitor.py detects a new position from a tracked wallet, this module:
  1. Parses the market title + outcome to identify the sport and side
  2. Fetches live Kalshi markets and finds the equivalent market
  3. Places a limit buy order proportional to the wallet's bet (capped at MAX_BET)
  4. Saves the mapping to state so we can close the Kalshi position on exit

When copy_monitor.py detects a wallet exit, this module finds and sells the
corresponding Kalshi position.

Requires env vars:
  KALSHI_API_KEY_ID      - UUID from Kalshi API key setup
  KALSHI_PRIVATE_KEY     - base64-encoded RSA private key PEM (for GitHub Actions)
  OR
  KALSHI_PRIVATE_KEY_PATH - path to PEM file (for local use)

Setup (one-time):
  python kalshi_auto_trader.py setup
"""

import base64, json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests

from kalshi_auth import get, post, delete, load_private_key

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
MAX_BET   = float(os.environ.get("MAX_BET", "20"))
MIN_BET   = 3.0
SLACK     = 0.02  # 2¢ slack on limit price to help ensure fill

# ── team name keyword → Kalshi team field fragment ─────────────────────────
# Kalshi truncates team names. We match by checking if any keyword from the
# Polymarket team name appears in the Kalshi team field (case-insensitive).
# Longer keywords are weighted higher to avoid false matches (e.g. "New" alone).

MLB_KEYWORDS = {
    "Arizona Diamondbacks": ["arizona", "diamondbacks"],
    "Atlanta Braves":        ["atlanta", "braves"],
    "Baltimore Orioles":     ["baltimore", "orioles"],
    "Boston Red Sox":        ["boston", "red sox"],
    "Chicago Cubs":          ["chicago", "cubs"],
    "Chicago White Sox":     ["chicago", "white sox"],
    "Cincinnati Reds":       ["cincinnati", "reds"],
    "Cleveland Guardians":   ["cleveland", "guardians"],
    "Colorado Rockies":      ["colorado", "rockies"],
    "Detroit Tigers":        ["detroit", "tigers"],
    "Houston Astros":        ["houston", "astros"],
    "Kansas City Royals":    ["kansas", "royals"],
    "Los Angeles Angels":    ["angels", "anaheim"],
    "Los Angeles Dodgers":   ["dodgers", "los angeles d"],
    "Miami Marlins":         ["miami", "marlins"],
    "Milwaukee Brewers":     ["milwaukee", "brewers"],
    "Minnesota Twins":       ["minnesota", "twins"],
    "New York Mets":         ["mets", "new york m"],
    "New York Yankees":      ["yankees", "new york y"],
    "Oakland Athletics":     ["athletics", "oakland", "a's"],
    "Athletics":             ["athletics", "a's"],
    "Philadelphia Phillies": ["philadelphia", "phillies"],
    "Pittsburgh Pirates":    ["pittsburgh", "pirates"],
    "San Diego Padres":      ["san diego", "padres"],
    "San Francisco Giants":  ["san francisco", "giants"],
    "Seattle Mariners":      ["seattle", "mariners"],
    "St. Louis Cardinals":   ["st. louis", "cardinals"],
    "Tampa Bay Rays":        ["tampa", "rays"],
    "Texas Rangers":         ["texas", "rangers"],
    "Toronto Blue Jays":     ["toronto", "blue jays"],
    "Washington Nationals":  ["washington", "nationals"],
}

MLS_KEYWORDS = {
    "LA Galaxy":             ["los angeles g", "la galaxy", "galaxy"],
    "LAFC":                  ["los angeles f", "lafc"],
    "LA FC":                 ["los angeles f", "lafc"],
    "Sporting Kansas City":  ["kansas city", "sporting kc", "skc"],
    "Portland Timbers":      ["portland"],
    "Seattle Sounders":      ["seattle"],
    "Colorado Rapids":       ["colorado"],
    "Real Salt Lake":        ["salt lake", "rsl"],
    "San Jose Earthquakes":  ["san jose"],
    "San Diego FC":          ["san diego"],
    "FC Dallas":             ["dallas"],
    "St. Louis City SC":     ["saint louis", "st. louis c"],
    "Nashville SC":          ["nashville"],
    "Orlando City":          ["orlando"],
    "Columbus Crew":         ["columbus"],
    "FC Cincinnati":         ["cincinnati"],
    "Atlanta United":        ["atlanta"],
    "Charlotte FC":          ["charlotte"],
    "Chicago Fire":          ["chicago"],
    "New York City FC":      ["new york c", "nycfc"],
    "New York Red Bulls":    ["new york r", "red bulls"],
    "New England Revolution": ["new england"],
    "Toronto FC":            ["toronto"],
    "CF Montreal":           ["montreal"],
    "Austin FC":             ["austin"],
    "Houston Dynamo":        ["houston"],
    "Minnesota United":      ["minnesota"],
    "Vancouver Whitecaps":   ["vancouver"],
    "Philadelphia Union":    ["philadelphia"],
    "DC United":             ["d.c.", "dc united"],
    "Inter Miami":           ["miami"],
}

ALL_KEYWORDS = {**MLB_KEYWORDS, **MLS_KEYWORDS}


def _write_key_from_env():
    """Write base64-encoded private key from env to temp file (for GitHub Actions)."""
    b64 = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not b64:
        return None
    path = Path("/tmp/kalshi_private_key.pem")
    path.write_bytes(base64.b64decode(b64))
    path.chmod(0o600)
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(path)
    return str(path)


def _ensure_key():
    if not os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        _write_key_from_env()


def kalshi_get(path, params=None):
    _ensure_key()
    return get(path, params)


def kalshi_post(path, body):
    _ensure_key()
    return post(path, body)


def kalshi_delete(path):
    _ensure_key()
    return delete(path)


# ── market fetching ─────────────────────────────────────────────────────────

# Kalshi series tickers for each sport
_SERIES = {
    "MLB":  ["KXMLBGAME", "KXMLBTOTAL"],
    "MLS":  ["KXMLSGAME", "KXLIGAMXGAME"],
    "NFL":  ["KXNFLGAME"],
    "UFC":  ["KXUFCFIGHT"],
}


def _fetch_events_for_sport(sport):
    """Return list of (event_ticker, event_title) for open events of a sport."""
    events = []
    for series in _SERIES.get(sport, []):
        cursor = ""
        for _ in range(5):
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            code, data = kalshi_get("/events", params)
            if code != 200:
                break
            batch = data.get("events", [])
            for e in batch:
                events.append({
                    "event_ticker": e.get("event_ticker", ""),
                    "event_title":  e.get("title", ""),
                    "series":       series,
                })
            cursor = data.get("cursor", "")
            if not cursor or len(batch) < 200:
                break
    return events


def _fetch_markets_for_event(event_ticker):
    """Fetch individual markets (with prices) for a specific event."""
    code, data = kalshi_get("/markets", {"event_ticker": event_ticker, "status": "open", "limit": 50})
    if code != 200:
        return []
    markets = []
    for m in data.get("markets", []):
        ticker = m.get("ticker", "")
        # Prices come back as *_dollars fields (0-1 range) when fetched individually
        code2, mdata = kalshi_get(f"/markets/{ticker}")
        if code2 == 200:
            markets.append(mdata.get("market", m))
        else:
            markets.append(m)
    return markets


# ── matching ────────────────────────────────────────────────────────────────

def _score_team_match(team_name, text):
    """Score how well a Polymarket team name matches arbitrary Kalshi text."""
    text_lower = text.lower()
    keywords = ALL_KEYWORDS.get(team_name, [team_name.lower().split()[0]])
    score = 0
    for kw in keywords:
        if kw in text_lower:
            score += len(kw)
    return score


def _detect_sport(title):
    """Guess sport from market title."""
    t = title.lower()
    for team in (k.lower() for k in MLB_KEYWORDS):
        if team in t:
            return "MLB"
    for team in (k.lower() for k in MLS_KEYWORDS):
        if team in t:
            return "MLS"
    if any(w in t for w in ["nfl", "super bowl", "patriots", "chiefs", "eagles"]):
        return "NFL"
    if any(w in t for w in ["ufc", "mma"]):
        return "UFC"
    return None


def _extract_teams(title):
    """Extract up to two team names from 'Team A vs. Team B' style title."""
    title_clean = re.sub(r':.*$', '', title).strip()
    parts = re.split(r'\s+vs\.?\s+', title_clean, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _find_kalshi_ticker(title, outcome, sport):
    """
    Find the Kalshi market ticker that matches this Polymarket trade.
    Returns (ticker, side, yes_price) or (None, None, None).
    yes_price is in 0-1 range (dollars).

    outcome: team name (e.g. "Houston Astros") OR "Over"/"Under"
    """
    events = _fetch_events_for_sport(sport)
    if not events:
        return None, None, None

    teams = _extract_teams(title)
    is_over_under = "O/U" in title.upper() or outcome in ("Over", "Under")

    # Step 1: find the best matching event by title
    # For O/U bets prefer TOTAL series; for game-winner prefer GAME/FIGHT series
    best_event  = None
    best_escore = 0
    for ev in events:
        if is_over_under and "TOTAL" not in ev.get("series", "").upper():
            continue
        if not is_over_under and "TOTAL" in ev.get("series", "").upper():
            continue
        ev_title = ev["event_title"]
        score = sum(_score_team_match(team, ev_title) for team in teams)
        if score > best_escore:
            best_escore = score
            best_event  = ev

    # Fallback: if no match found with strict filter, try any series
    if not best_event or best_escore == 0:
        for ev in events:
            ev_title = ev["event_title"]
            score = sum(_score_team_match(team, ev_title) for team in teams)
            if score > best_escore:
                best_escore = score
                best_event  = ev

    if not best_event or best_escore == 0:
        return None, None, None

    # Step 2: fetch individual markets for that event
    markets = _fetch_markets_for_event(best_event["event_ticker"])
    if not markets:
        return None, None, None

    if is_over_under:
        # Extract the O/U line from the title (e.g. "O/U 8.5" → 8.5)
        line_m = re.search(r'[Oo][/\\]?[Uu]\s*([\d]+\.[\d]+)', title)
        ou_line = float(line_m.group(1)) if line_m else None

        is_over = outcome == "Over"
        best_m, best_diff = None, float("inf")
        for m in markets:
            # Prefer floor_strike field; fall back to parsing yes_sub_title
            strike = m.get("floor_strike")
            if strike is None:
                sub_m = re.search(r'[Oo]ver\s*([\d]+\.[\d]+)',
                                  m.get("yes_sub_title") or "")
                if sub_m:
                    strike = float(sub_m.group(1))
            if strike is None:
                continue
            diff = abs(float(strike) - ou_line) if ou_line is not None else 0
            if diff < best_diff:
                best_diff = diff
                best_m = m

        if not best_m:
            return None, None, None
        if is_over:
            price = float(best_m.get("yes_ask_dollars") or best_m.get("yes_ask") or 0.5)
            return best_m.get("ticker"), "yes", price
        else:
            price = float(best_m.get("no_ask_dollars") or best_m.get("no_ask") or 0.5)
            return best_m.get("ticker"), "no", price

    # Step 3: within the event's markets, find the one where YES = outcome team
    best_ticker = None
    best_mscore = 0
    best_price  = None
    for m in markets:
        yes_sub = m.get("yes_sub_title") or ""
        yes_ask = m.get("yes_ask_dollars") or m.get("yes_ask")
        if not yes_ask:
            continue
        yes_ask = float(yes_ask)
        score = _score_team_match(outcome, yes_sub) if outcome not in ("Yes","No") else 1
        if score > best_mscore:
            best_mscore = score
            best_ticker = m.get("ticker")
            best_price  = yes_ask

    if best_mscore == 0:
        return None, None, None
    return best_ticker, "yes", best_price


# ── order execution ─────────────────────────────────────────────────────────

def execute_trade(title, outcome, entry_price, their_dollars, condition_id,
                  kalshi_positions, log_fn=print):
    """
    Find equivalent Kalshi market and place a proportional buy order.
    Updates kalshi_positions dict with {condition_id: {ticker, count, side}}.
    Returns True on success.
    """
    if not os.environ.get("KALSHI_API_KEY_ID"):
        log_fn("  [kalshi] KALSHI_API_KEY_ID not set — skipping auto-trade")
        return False

    sport = _detect_sport(title)
    if not sport:
        log_fn(f"  [kalshi] can't detect sport from: {title[:60]}")
        return False

    ticker, side, ask_price = _find_kalshi_ticker(title, outcome, sport)

    if not ticker:
        log_fn(f"  [kalshi] no Kalshi market found for: {title[:60]}")
        return False

    # Proportional sizing: match their bet, cap at MAX_BET
    our_bet = min(MAX_BET, their_dollars)
    if our_bet < MIN_BET:
        log_fn(f"  [kalshi] bet ${our_bet:.2f} below minimum ${MIN_BET} — skip")
        return False

    # V2 API: side="bid"(buy YES) or "ask"(buy NO=sell YES at complement)
    # price is in dollars as a string; count is a string with 2 decimal places
    raw_ask = ask_price or 0.55
    if side == "yes":
        api_side    = "bid"
        limit_price = min(0.97, raw_ask + SLACK)
        contracts   = max(1, int(our_bet / limit_price))
    else:
        # Buy NO: post a YES ask at (1 - no_ask - slack) to match NO buyers
        api_side    = "ask"
        limit_price = max(0.03, 1 - raw_ask - SLACK)
        contracts   = max(1, int(our_bet / raw_ask))

    log_fn(f"  [kalshi] {ticker} — BUY {contracts} {side.upper()} @ ${limit_price:.2f} (~${our_bet:.0f})")

    code, resp = kalshi_post("/portfolio/events/orders", {
        "ticker":                    ticker,
        "side":                      api_side,
        "count":                     f"{contracts:.2f}",
        "price":                     f"{limit_price:.4f}",
        "client_order_id":           f"copy-{condition_id[:8]}-{int(time.time())}",
        "time_in_force":             "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    })

    if code in (200, 201):
        filled = resp.get("fill_count") or resp.get("count") or contracts
        log_fn(f"  [kalshi] order placed — filled ~{filled} contracts")
        kalshi_positions[condition_id] = {
            "ticker":       ticker,
            "contracts":    contracts,
            "side":         side,
            "entry_price":  limit_price,
            "placed_at":    datetime.now().isoformat(timespec="seconds"),
        }
        return True
    else:
        log_fn(f"  [kalshi] order FAILED — HTTP {code}: {resp}")
        return False


def close_trade(condition_id, kalshi_positions, log_fn=print):
    """Sell the Kalshi position associated with a Polymarket condition_id."""
    pos = kalshi_positions.get(condition_id)
    if not pos:
        return False

    if not os.environ.get("KALSHI_API_KEY_ID"):
        return False

    ticker    = pos["ticker"]
    contracts = pos.get("contracts", 1)
    side      = pos.get("side", "yes")

    # Get current bid to set sell limit
    code, market_data = kalshi_get(f"/markets/{ticker}")
    limit_sell = 0.50
    if code == 200:
        mkt = market_data.get("market", {})
        if side == "no":
            no_bid     = float(mkt.get("no_bid_dollars") or mkt.get("no_bid") or 0.5)
            api_side   = "bid"
            # Sell NO = buy YES at complement of no_bid; bid up slightly to ensure fill
            limit_sell = min(0.97, 1 - no_bid + SLACK)
        else:
            yes_bid    = float(mkt.get("yes_bid_dollars") or mkt.get("yes_bid") or 0.5)
            api_side   = "ask"
            # Sell YES: post ask slightly below current bid to ensure fill
            limit_sell = max(0.03, yes_bid - SLACK)
    else:
        api_side = "ask" if side == "yes" else "bid"

    log_fn(f"  [kalshi] SELL {contracts} {side.upper()} on {ticker} @ ${limit_sell:.2f}")
    code, resp = kalshi_post("/portfolio/events/orders", {
        "ticker":                    ticker,
        "side":                      api_side,
        "count":                     f"{contracts:.2f}",
        "price":                     f"{limit_sell:.4f}",
        "client_order_id":           f"exit-{condition_id[:8]}-{int(time.time())}",
        "time_in_force":             "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    })

    if code in (200, 201):
        log_fn(f"  [kalshi] sell order placed for {ticker}")
        del kalshi_positions[condition_id]
        return True
    else:
        log_fn(f"  [kalshi] sell FAILED — HTTP {code}: {resp}")
        return False


# ── stop loss ────────────────────────────────────────────────────────────────

STOP_LOSS    = 0.75  # close individual position if it loses >= this fraction
KILL_SWITCH  = 0.75  # halt all new trades if account drops >= this fraction from start

def check_stop_losses(kalshi_positions, log_fn=print):
    """Close any open Kalshi position that has lost >= STOP_LOSS of its entry value."""
    if not os.environ.get("KALSHI_API_KEY_ID"):
        return
    to_close = []
    for condition_id, pos in list(kalshi_positions.items()):
        ticker      = pos.get("ticker")
        entry_price = pos.get("entry_price") or (pos.get("entry_cents", 0) / 100)
        side        = pos.get("side", "yes")
        if not ticker or not entry_price:
            continue
        code, data = kalshi_get(f"/markets/{ticker}")
        if code != 200:
            continue
        mkt = data.get("market", {})
        if side == "no":
            bid_val = float(mkt.get("no_bid_dollars") or mkt.get("no_bid") or 0)
        else:
            bid_val = float(mkt.get("yes_bid_dollars") or mkt.get("yes_bid") or 0)
        stop_price = entry_price * (1 - STOP_LOSS)
        if bid_val <= stop_price:
            log_fn(f"  [kalshi] STOP LOSS {ticker} — entry ${entry_price:.2f}, now ${bid_val:.2f}")
            to_close.append(condition_id)
    for cid in to_close:
        close_trade(cid, kalshi_positions, log_fn=log_fn)


# ── account kill switch ───────────────────────────────────────────────────────

def is_kill_switch_active(state, log_fn=print):
    """Return True and halt new trades if account is down >= KILL_SWITCH from start."""
    bal = get_balance()
    if bal is None:
        return False
    if "kalshi_starting_balance" not in state:
        state["kalshi_starting_balance"] = bal
        log_fn(f"  [kalshi] starting balance recorded: ${bal:.2f}")
        return False
    start = state["kalshi_starting_balance"]
    if start > 0 and bal <= start * (1 - KILL_SWITCH):
        log_fn(f"  [kalshi] KILL SWITCH — balance ${bal:.2f} is >{KILL_SWITCH:.0%} below "
               f"starting ${start:.2f}. No new trades until manually reset.")
        return True
    return False


# ── balance check ────────────────────────────────────────────────────────────

def get_balance():
    _ensure_key()
    code, data = kalshi_get("/portfolio/balance")
    if code == 200:
        bal = data.get("balance", 0)
        if isinstance(bal, (int, float)):
            return bal / 100
        return float(data.get("balance_dollars", 0))
    return None


# ── setup / test CLI ─────────────────────────────────────────────────────────

def cmd_setup():
    print("""
KALSHI AUTO-TRADER SETUP (one-time, ~5 min)
============================================

1. Generate RSA keypair:
   mkdir -p ~/.kalshi
   openssl genrsa -out ~/.kalshi/private_key.pem 2048
   openssl rsa -in ~/.kalshi/private_key.pem -pubout -out ~/.kalshi/public_key.pem
   chmod 600 ~/.kalshi/private_key.pem

2. Upload public key to Kalshi:
   Go to: app.kalshi.com → Account → API Keys → Create New
   Paste contents of: cat ~/.kalshi/public_key.pem
   Copy the "Access Key ID" UUID they show you.

3. Set env vars (add to ~/.zshrc):
   export KALSHI_API_KEY_ID=<uuid>
   export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/private_key.pem

4. Add to GitHub Secrets:
   KALSHI_API_KEY_ID  = <uuid>
   KALSHI_PRIVATE_KEY = $(base64 -i ~/.kalshi/private_key.pem)

5. Test:
   python kalshi_auto_trader.py test
""")


def cmd_test():
    _ensure_key()
    bal = get_balance()
    if bal is not None:
        print(f"Kalshi auth OK. Available balance: ${bal:.2f}")
    else:
        print("Auth failed — check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        return

    # Test match
    test_title   = "Houston Astros vs. Chicago White Sox"
    test_outcome = "Houston Astros"
    ticker, side, price = _find_kalshi_ticker(test_title, test_outcome, "MLB")
    if ticker:
        print(f"Test match: '{test_title}' → {ticker} @ {int((price or 0)*100)}¢")
    else:
        print(f"Test match: '{test_title}' → NO MATCH (may not be an active game)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        cmd_test()
    else:
        cmd_setup()
