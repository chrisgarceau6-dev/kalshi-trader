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
SLACK_CENTS = 2  # place limit MAX(ask, last_price) + SLACK_CENTS to ensure fill

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

def _fetch_markets(sport_prefix, date_str=None):
    """Fetch open Kalshi markets for a sport. date_str = 'YYYYMMDD'."""
    params = {"status": "open", "limit": 200}
    if date_str:
        params["event_ticker"] = f"KX{sport_prefix}GAME"
    code, data = kalshi_get("/markets", params)
    if code != 200:
        return []
    markets = data.get("markets", [])
    results = []
    for m in markets:
        ticker = m.get("ticker", "")
        if sport_prefix.upper() not in ticker.upper():
            continue
        results.append(m)
    return results


def _fetch_all_sports_markets():
    """Fetch all open game markets (MLB + MLS + NFL + UFC)."""
    all_markets = []
    code, data = kalshi_get("/markets", {"status": "open", "limit": 1000})
    if code == 200:
        for m in data.get("markets", []):
            t = m.get("ticker", "")
            if any(s in t for s in ["MLBGAME", "MLSGAME", "NFLGAME", "UFCGAME"]):
                all_markets.append(m)
    return all_markets


# ── matching ────────────────────────────────────────────────────────────────

def _score_team_match(team_name, kalshi_subtitle):
    """Score how well a Polymarket team name matches a Kalshi market subtitle."""
    subtitle_lower = kalshi_subtitle.lower()
    keywords = ALL_KEYWORDS.get(team_name, [team_name.lower().split()[0]])
    score = 0
    for kw in keywords:
        if kw in subtitle_lower:
            score += len(kw)
    return score


def _detect_sport(title):
    """Guess sport from market title."""
    t = title.lower()
    mlb_teams = set(k.lower() for k in MLB_KEYWORDS)
    mls_teams = set(k.lower() for k in MLS_KEYWORDS)
    for team in mlb_teams:
        if team in t:
            return "MLB"
    for team in mls_teams:
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


def _find_kalshi_ticker(title, outcome, markets):
    """
    Find the Kalshi market ticker that matches this Polymarket trade.
    Returns (ticker, side, yes_price_cents) or (None, None, None).

    outcome: team name (e.g. "Houston Astros") OR "Over"/"Under" OR "Yes"/"No"
    """
    if not markets:
        return None, None, None

    teams = _extract_teams(title)
    is_over_under = "O/U" in title.upper() or outcome in ("Over", "Under")

    best_ticker  = None
    best_score   = 0
    best_price   = None

    for m in markets:
        ticker    = m.get("ticker", "")
        subtitle  = m.get("subtitle", "") or m.get("title", "")
        yes_ask   = m.get("yes_ask")
        yes_bid   = m.get("yes_bid")

        if is_over_under:
            if "OVER" in ticker.upper() or "UNDER" in ticker.upper():
                kw = "over" if outcome == "Over" else "under"
                if kw in ticker.lower():
                    price = yes_ask or 50
                    if price is not None and price * 100 > best_score:
                        best_score  = price * 100
                        best_ticker = ticker
                        best_price  = price
            continue

        for team in teams:
            score = _score_team_match(team, subtitle)
            if score <= 0:
                continue
            # Check if this team IS the outcome we want to bet on
            outcome_score = _score_team_match(outcome if outcome not in ("Yes","No") else team,
                                              subtitle)
            if outcome not in ("Yes","No") and _score_team_match(outcome, subtitle) == 0:
                continue
            total_score = score + outcome_score
            if total_score > best_score and yes_ask is not None:
                best_score  = total_score
                best_ticker = ticker
                best_price  = yes_ask

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

    markets = _fetch_all_sports_markets()
    ticker, side, ask_price = _find_kalshi_ticker(title, outcome, markets)

    if not ticker:
        log_fn(f"  [kalshi] no Kalshi market found for: {title[:60]}")
        return False

    # Proportional sizing: match their bet, cap at MAX_BET
    our_bet = min(MAX_BET, their_dollars)
    if our_bet < MIN_BET:
        log_fn(f"  [kalshi] bet ${our_bet:.2f} below minimum ${MIN_BET} — skip")
        return False

    # Limit price = ask + slack, capped at 97 cents
    limit_cents = min(97, int((ask_price or 0.55) * 100) + SLACK_CENTS)
    contracts   = max(1, int(our_bet / (limit_cents / 100)))

    log_fn(f"  [kalshi] {ticker} — BUY {contracts} YES @ {limit_cents}¢ (~${our_bet:.0f})")

    code, resp = kalshi_post("/portfolio/orders", {
        "ticker":          ticker,
        "type":            "limit",
        "action":          "buy",
        "side":            side,
        "count":           contracts,
        "yes_price":       limit_cents,
        "client_order_id": f"copy-{condition_id[:8]}-{int(time.time())}",
        "time_in_force":   "GTC",
    })

    if code in (200, 201):
        order = resp.get("order", {})
        filled = order.get("contracts_filled", contracts)
        log_fn(f"  [kalshi] order placed — filled {filled}/{contracts} contracts")
        kalshi_positions[condition_id] = {
            "ticker":      ticker,
            "contracts":   filled or contracts,
            "side":        side,
            "entry_cents": limit_cents,
            "placed_at":   datetime.now().isoformat(timespec="seconds"),
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

    # Get current bid to set sell limit
    code, market_data = kalshi_get(f"/markets/{ticker}")
    bid_cents = 50
    if code == 200:
        bid_cents = int((market_data.get("market", {}).get("yes_bid", 0.5) or 0.5) * 100)
    limit_sell = max(3, bid_cents - SLACK_CENTS)

    log_fn(f"  [kalshi] SELL {contracts} YES on {ticker} @ {limit_sell}¢")
    code, resp = kalshi_post("/portfolio/orders", {
        "ticker":          ticker,
        "type":            "limit",
        "action":          "sell",
        "side":            "yes",
        "count":           contracts,
        "yes_price":       limit_sell,
        "client_order_id": f"exit-{condition_id[:8]}-{int(time.time())}",
        "time_in_force":   "GTC",
    })

    if code in (200, 201):
        log_fn(f"  [kalshi] sell order placed for {ticker}")
        del kalshi_positions[condition_id]
        return True
    else:
        log_fn(f"  [kalshi] sell FAILED — HTTP {code}: {resp}")
        return False


# ── stop loss ────────────────────────────────────────────────────────────────

STOP_LOSS = 0.75  # close if position loses >= this fraction of entry value

def check_stop_losses(kalshi_positions, log_fn=print):
    """Close any open Kalshi position that has lost >= STOP_LOSS of its entry value."""
    if not os.environ.get("KALSHI_API_KEY_ID"):
        return
    to_close = []
    for condition_id, pos in list(kalshi_positions.items()):
        ticker      = pos.get("ticker")
        entry_cents = pos.get("entry_cents")
        if not ticker or not entry_cents:
            continue
        code, data = kalshi_get(f"/markets/{ticker}")
        if code != 200:
            continue
        bid_cents  = int((data.get("market", {}).get("yes_bid", 0) or 0) * 100)
        stop_price = max(1, int(entry_cents * (1 - STOP_LOSS)))
        if bid_cents <= stop_price:
            log_fn(f"  [kalshi] STOP LOSS {ticker} — entry {entry_cents}c, now {bid_cents}c")
            to_close.append(condition_id)
    for cid in to_close:
        close_trade(cid, kalshi_positions, log_fn=log_fn)


# ── balance check ────────────────────────────────────────────────────────────

def get_balance():
    _ensure_key()
    code, data = kalshi_get("/portfolio/balance")
    if code == 200:
        return data.get("balance", {}).get("available_balance_cents", 0) / 100
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

    markets = _fetch_all_sports_markets()
    print(f"Open game markets: {len(markets)}")

    # Test match
    test_title   = "Houston Astros vs. Chicago White Sox"
    test_outcome = "Houston Astros"
    ticker, side, price = _find_kalshi_ticker(test_title, test_outcome, markets)
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
