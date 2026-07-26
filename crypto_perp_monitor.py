#!/usr/bin/env python3
"""Crypto perp auto-trader: copies BTC/ETH directional bets from sharp Polymarket
wallets into Kalshi perpetual futures positions.

Tracks wallets that trade "Bitcoin Up or Down" 5-minute window markets (97%+ win rate).
When they open a new Up/Down position, opens a matching Kalshi BTC perp long/short.
Auto-closes after HOLD_MINUTES to capture the signal before the window resolves.

Usage:
    python crypto_perp_monitor.py          # run daemon (polls every 45s)
    python crypto_perp_monitor.py --once   # single poll
    python crypto_perp_monitor.py --test   # check auth + balance
"""

import argparse, json, os, time, base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── config ───────────────────────────────────────────────────────────────────

PERP_BASE   = "https://external-api.kalshi.com/trade-api/v2"
DATA_BASE   = "https://data-api.polymarket.com"
STATE_FILE  = Path(__file__).parent / "perp_state.json"
LOG_FILE    = Path(__file__).parent / "perp_monitor.log"

POLL_INTERVAL  = 45       # seconds between polls
HOLD_MINUTES   = 7        # auto-close perp after this many minutes
PERP_NOTIONAL  = float(os.environ.get("PERP_MAX_BET", "20"))   # dollars per trade
KILL_LOSS_PCT  = 0.50     # halt if perp account drops 50% from start

# Sharp crypto wallets — 94%+ win rate on BTC Up/Down markets
CRYPTO_WALLETS = {
    "btc-sharp-1": "0xaa9621853f92c2dd3be06871b352bce365ef6969",
    "btc-sharp-2": "0xa191f2791f6dc0adf74c4d9223ae7df3cdf6b426",
    "btc-sharp-3": "0x85d379d0ed5ae9077e96fd97938e38a245816be3",
    "btc-sharp-4": "0x8f6924adcc039b6f42c9b94751b36df65aab2039",
}

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# ── auth ─────────────────────────────────────────────────────────────────────

def _load_private_key():
    b64 = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if b64:
        path = Path("/tmp/kalshi_private_key.pem")
        path.write_bytes(base64.b64decode(b64))
        path.chmod(0o600)
        os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(path)
    key_path = Path(os.path.expanduser(
        os.environ.get("KALSHI_PRIVATE_KEY_PATH", "~/.kalshi/private_key.pem")
    ))
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

_pk = None
def _get_pk():
    global _pk
    if _pk is None:
        _pk = _load_private_key()
    return _pk


def _signed_headers(method, path):
    api_key = os.environ.get("KALSHI_API_KEY_ID", "")
    ts  = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = _get_pk().sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Accept":                  "application/json",
        "Content-Type":            "application/json",
    }


def perp_get(path, params=None):
    hdrs = _signed_headers("GET", path)
    r = requests.get(PERP_BASE + path, headers=hdrs, params=params, timeout=15)
    return r.status_code, r.json() if r.content else {}


def perp_post(path, body):
    hdrs = _signed_headers("POST", path)
    r = requests.post(PERP_BASE + path, headers=hdrs, json=body, timeout=15)
    return r.status_code, r.json() if r.content else {}


# ── polymarket ───────────────────────────────────────────────────────────────

def fetch_positions(wallet):
    try:
        r = requests.get(f"{DATA_BASE}/positions",
                         params={"user": wallet, "limit": 500},
                         timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


def get_direction(title, outcome):
    """Return 'up', 'down', or None from a BTC/ETH Up or Down market."""
    if "up or down" not in title.lower():
        return None
    out = (outcome or "").lower()
    if "up" in out:
        return "up"
    if "down" in out:
        return "down"
    return None


def get_asset(title):
    t = title.lower()
    if "bitcoin" in t or "btc" in t:
        return "BTC"
    if "ethereum" in t or "eth" in t:
        return "ETH"
    return None


# ── perp market info ─────────────────────────────────────────────────────────

TICKER_MAP = {"BTC": "KXBTCPERP", "ETH": "KXETHPERP"}


def get_perp_price(asset):
    """Return (bid, ask) for a perp asset."""
    ticker = TICKER_MAP.get(asset)
    if not ticker:
        return None, None
    code, data = perp_get(f"/margin/markets/{ticker}")
    if code != 200:
        return None, None
    m = data.get("market", data)
    return float(m.get("bid") or 0), float(m.get("ask") or 0)


def get_perp_balance():
    code, data = perp_get("/margin/balance")
    if code == 200:
        bal = data.get("balance", {})
        return float(bal.get("available_balance") or bal.get("cash") or 0)
    return None


# ── order placement ──────────────────────────────────────────────────────────

def open_perp(asset, direction, notional_dollars, signal_id, log_fn=print):
    """Open a perp position. direction='up'→long(bid), 'down'→short(ask)."""
    ticker   = TICKER_MAP.get(asset)
    if not ticker:
        log_fn(f"  [perp] no perp market for {asset}")
        return None

    bid, ask = get_perp_price(asset)
    if not ask:
        log_fn(f"  [perp] could not get price for {ticker}")
        return None

    # Long = bid side (buy), Short = ask side (sell)
    side  = "bid" if direction == "up" else "ask"
    price = ask if direction == "up" else bid

    # Contract size: KXBTCPERP = 0.0001 BTC
    code2, mdata = perp_get(f"/margin/markets/{ticker}")
    contract_size = float(mdata.get("market", {}).get("contract_size") or 0.0001)
    contract_value = price * contract_size if price else 1

    count = max(1, int(notional_dollars / contract_value))
    price_str = f"{price:.4f}"

    log_fn(f"  [perp] {ticker} {direction.upper()} {count} contracts @ ${price_str} (notional ~${count*contract_value:.0f})")

    code, resp = perp_post("/margin/orders", {
        "ticker":                    ticker,
        "client_order_id":           f"perp-{signal_id[:8]}-{int(time.time())}",
        "side":                      side,
        "count":                     str(count),
        "price":                     price_str,
        "time_in_force":             "immediate_or_cancel",
        "self_trade_prevention_type": "cancel_resting",
    })

    if code in (200, 201):
        order = resp.get("order", {})
        filled = order.get("filled_count") or order.get("count", count)
        order_id = order.get("order_id", "")
        log_fn(f"  [perp] opened {direction.upper()} position: {filled} contracts, order_id={order_id}")
        return {
            "order_id":      order_id,
            "ticker":        ticker,
            "asset":         asset,
            "direction":     direction,
            "count":         int(filled or count),
            "entry_price":   price,
            "contract_size": contract_size,
            "opened_at":     datetime.now(timezone.utc).isoformat(),
            "close_at":      (datetime.now(timezone.utc) + timedelta(minutes=HOLD_MINUTES)).isoformat(),
            "signal_id":     signal_id,
        }
    else:
        log_fn(f"  [perp] order FAILED HTTP {code}: {resp}")
        return None


def close_perp(pos, log_fn=print):
    """Close a perp position by placing the opposite side at market."""
    ticker    = pos.get("ticker")
    count     = pos.get("count", 1)
    direction = pos.get("direction")

    bid, ask = get_perp_price(pos.get("asset", "BTC"))
    if not bid and not ask:
        log_fn(f"  [perp] could not get price to close {ticker}")
        return False

    # To close: reverse side
    close_side  = "ask" if direction == "up" else "bid"
    close_price = bid if direction == "up" else ask
    price_str   = f"{close_price:.4f}"

    log_fn(f"  [perp] closing {direction.upper()} {count} contracts on {ticker} @ ${price_str}")

    code, resp = perp_post("/margin/orders", {
        "ticker":                    ticker,
        "client_order_id":           f"close-{pos.get('signal_id','x')[:8]}-{int(time.time())}",
        "side":                      close_side,
        "count":                     str(count),
        "price":                     price_str,
        "time_in_force":             "immediate_or_cancel",
        "self_trade_prevention_type": "cancel_resting",
    })
    if code in (200, 201):
        log_fn(f"  [perp] position closed")
        return True
    else:
        log_fn(f"  [perp] close FAILED HTTP {code}: {resp}")
        return False


# ── state ────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"wallets": {}, "open_perps": [], "starting_balance": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── notifications ─────────────────────────────────────────────────────────────

def notify(title, body):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data=body.encode(),
                      headers={"Title": title, "Priority": "high", "Tags": "chart_increasing"},
                      timeout=8)
    except Exception:
        pass


# ── logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── main poll ────────────────────────────────────────────────────────────────

def poll_once(state):
    now = datetime.now(timezone.utc)

    # 1. Auto-close expired perp positions
    still_open = []
    for pos in state.get("open_perps", []):
        close_at = datetime.fromisoformat(pos["close_at"])
        if close_at.tzinfo is None:
            close_at = close_at.replace(tzinfo=timezone.utc)
        if now >= close_at:
            log(f"  [perp] time to close {pos['direction'].upper()} {pos['asset']} (held {HOLD_MINUTES}min)")
            closed = close_perp(pos, log_fn=log)
            if not closed:
                still_open.append(pos)
            else:
                notify(f"PERP CLOSED: {pos['direction'].upper()} {pos['asset']}",
                       f"Held {HOLD_MINUTES}min auto-close")
        else:
            still_open.append(pos)
    state["open_perps"] = still_open

    # 2. Kill switch: check perp balance
    bal = get_perp_balance()
    if bal is not None:
        if state["starting_balance"] is None:
            state["starting_balance"] = bal
            log(f"  [perp] starting balance: ${bal:.2f}")
        elif state["starting_balance"] > 0 and bal <= state["starting_balance"] * (1 - KILL_LOSS_PCT):
            log(f"  [perp] KILL SWITCH: balance ${bal:.2f} < {KILL_LOSS_PCT:.0%} of starting ${state['starting_balance']:.2f}")
            save_state(state)
            return

    # 3. Poll wallets for new crypto direction signals
    for label, wallet in CRYPTO_WALLETS.items():
        prev_keys = set(state["wallets"].get(wallet, {}).keys())
        positions = fetch_positions(wallet)

        current = {}
        for p in positions:
            key  = f"{p.get('conditionId')}:{p.get('asset')}"
            size = float(p.get("size") or 0)
            if size > 0:
                title   = (p.get("title") or "")[:120]
                outcome = p.get("outcome") or ""
                current[key] = {"title": title, "outcome": outcome,
                                 "conditionId": p.get("conditionId")}

        if not prev_keys:
            log(f"  [{label}] first poll — baseline {len(current)} positions")
            state["wallets"][wallet] = current
            save_state(state)
            continue

        new_keys = set(current.keys()) - prev_keys
        for k in new_keys:
            pos     = current[k]
            title   = pos["title"]
            outcome = pos["outcome"]
            asset   = get_asset(title)
            direction = get_direction(title, outcome)

            if not asset or not direction:
                continue

            log(f"  [{label}] SIGNAL: {asset} {direction.upper()} — {title[:60]}")

            if bal is None or (state["starting_balance"] and bal <= state["starting_balance"] * (1 - KILL_LOSS_PCT)):
                log("  [perp] kill switch active — skipping trade")
                continue

            perp_pos = open_perp(asset, direction, PERP_NOTIONAL,
                                  pos["conditionId"], log_fn=log)
            if perp_pos:
                state["open_perps"].append(perp_pos)
                notify(f"PERP {direction.upper()}: {asset} ({label})",
                       f"{title[:80]}\nOpened {perp_pos['count']} contracts @ ${perp_pos['entry_price']:.4f}\nAuto-close in {HOLD_MINUTES}min")

        state["wallets"][wallet] = current
        save_state(state)
        time.sleep(0.5)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once",   action="store_true")
    p.add_argument("--test",   action="store_true")
    p.add_argument("--daemon", action="store_true")
    a = p.parse_args()

    if a.test:
        bal = get_perp_balance()
        if bal is not None:
            print(f"Perp auth OK. Balance: ${bal:.2f}")
            bid, ask = get_perp_price("BTC")
            btc_price = ask / 0.0001 if ask else 0
            print(f"BTC perp: bid=${bid:.4f} ask=${ask:.4f} (~${btc_price:,.0f}/BTC)")
        else:
            print("Auth failed — check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        return

    state = load_state()

    if a.once:
        poll_once(state)
        return

    log("=== CRYPTO PERP MONITOR STARTED ===")
    log(f"Watching {len(CRYPTO_WALLETS)} wallets | poll every {POLL_INTERVAL}s | hold {HOLD_MINUTES}min | ${PERP_NOTIONAL}/trade")
    while True:
        try:
            poll_once(state)
        except Exception as e:
            log(f"poll error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
