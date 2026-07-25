#!/usr/bin/env python3
"""Copy-trade monitor for Option B (4-wallet portfolio).

Polls Polymarket's data-api every 5 min, detects new positions opened by
each wallet, alerts you with:
  - which wallet opened it
  - market question
  - entry price (avg cost)
  - their real position size
  - your recommended $30 copy trade
  - direct Polymarket link

Also detects exits (positions closed) so you know when to sell your copy.

Runs as a daemon; state saved between polls in copy_state.json.

usage:
    python copy_monitor.py --daemon
    python copy_monitor.py --once      # single poll (test)
"""
import argparse, json, os, smtplib, subprocess, sys, time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
import requests

BASE = Path(__file__).parent
STATE = BASE / "copy_state.json"
LOG = BASE / "copy_alerts.log"
DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

# Option B portfolio
WALLETS = {
    "workhorse":  "0x412fe1a101554f0b382181c3af932e4b2d8030fa",
    "hugewinner": "0xc8ec6d4cef5c5fe8409ef69303c37f05b678e8f1",
    "fbf-safe":   "0xfbf3d501e88815464642d0e913f15379c3eeb218",
    "new-anchor": "0xaa501f4663c454e94fba8907c42dafcc785a3ed5",
}
BET_SIZE = 30       # dollars per copy trade
POLL_INTERVAL = 300  # 5 min

# Notification: email if configured, else macOS notification
def send_email(subject, body):
    to_addr = os.environ.get("COPY_EMAIL_TO", "")
    from_addr = os.environ.get("COPY_EMAIL_FROM", "")
    password = os.environ.get("COPY_EMAIL_PASSWORD", "")
    if not (to_addr and from_addr and password):
        return False
    try:
        msg = MIMEMultipart()
        msg['From'] = from_addr
        msg['To'] = to_addr
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP('smtp.gmail.com', 587) as s:
            s.starttls()
            s.login(from_addr, password)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"email send failed: {e}", flush=True)
        return False


def notify_mac(title, body):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}" sound name "Ping"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def notify(title, body, detail=""):
    """Send email if configured, else Mac notification."""
    email_body = f"{body}\n\n{detail}" if detail else body
    if not send_email(f"[Copy Trade] {title}", email_body):
        notify_mac(title, body)


def log(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")


def fetch_positions(wallet):
    """All positions (open + resolved-unredeemed) for a wallet."""
    out, offset = [], 0
    for _ in range(20):
        try:
            r = requests.get(f"{DATA}/positions",
                             params={"user": wallet, "limit": 500, "offset": offset},
                             timeout=20)
            if r.status_code != 200: break
            batch = r.json()
            if not batch: break
            out.extend(batch)
            if len(batch) < 500: break
            offset += 500
            time.sleep(0.2)
        except Exception as e:
            log(f"  fetch error for {wallet[:10]}: {e}")
            break
    return out


def market_slug(condition_id):
    """Look up market slug for URL. Cached in-memory per run."""
    try:
        r = requests.get(f"{GAMMA}/markets", params={"condition_ids": condition_id, "limit": 1}, timeout=10)
        j = r.json()
        if isinstance(j, list) and j:
            return j[0].get("slug","")
        return ""
    except: return ""


def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except: pass
    return {}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, default=str))


def summarize_position(p):
    """Turn a position dict into readable fields."""
    return {
        "conditionId": p.get("conditionId"),
        "asset": p.get("asset"),
        "size": float(p.get("size") or 0),
        "avgPrice": float(p.get("avgPrice") or 0),
        "current_value": float(p.get("currentValue") or 0),
        "outcome": p.get("outcome"),
        "title": (p.get("title") or "")[:80],
        "endDate": p.get("endDate"),
    }


def poll_once(state):
    """Check all wallets, alert on new positions, update state."""
    log(f"=== POLL @ {datetime.now().strftime('%H:%M:%S')} ===")
    new_alerts = 0
    for label, wallet in WALLETS.items():
        prev = set(state.get(wallet, {}).get("positions", {}).keys())
        positions = fetch_positions(wallet)
        current = {}
        for p in positions:
            key = f"{p.get('conditionId')}:{p.get('asset')}"
            if float(p.get("size") or 0) > 0:
                current[key] = summarize_position(p)

        # Detect NEW positions
        new_keys = set(current.keys()) - prev
        # Detect EXITED positions (were there, now gone or size 0)
        exited_keys = prev - set(current.keys())

        if new_keys or exited_keys:
            log(f"  [{label}] {wallet[:12]}: {len(current)} open ({len(new_keys)} NEW, {len(exited_keys)} EXITED)")

        for k in new_keys:
            pos = current[k]
            # Compute your copy sizing: buy $30 worth at their avg price
            entry = pos["avgPrice"]
            your_shares = round(BET_SIZE / entry, 1) if entry > 0 else 0
            slug = market_slug(pos["conditionId"])
            url = f"https://polymarket.com/event/{slug}" if slug else pos["conditionId"]
            alert = (f"\n{'='*70}\n"
                     f"🎯 NEW COPY TRADE — {label} ({wallet[:10]})\n"
                     f"Market: {pos['title']}\n"
                     f"Their side: {pos['outcome']} @ avg ${entry:.3f}\n"
                     f"Their size: {pos['size']:.0f} shares (${pos['size']*entry:.0f})\n"
                     f"YOUR COPY: Buy {your_shares} shares of {pos['outcome']} at ~${entry:.3f} (~${BET_SIZE})\n"
                     f"URL: {url}\n"
                     f"{'='*70}")
            log(alert)
            notify(
                title=f"NEW: {label} → {pos['outcome']} @ ${entry:.3f}",
                body=f"Buy {your_shares} shares for ${BET_SIZE}\n{pos['title']}",
                detail=(f"Market: {pos['title']}\n"
                        f"Wallet: {label} ({wallet})\n"
                        f"Their side: {pos['outcome']} @ ${entry:.3f}\n"
                        f"Their size: {pos['size']:.0f} shares (~${pos['size']*entry:.0f})\n\n"
                        f"YOUR COPY: Buy {your_shares} shares of {pos['outcome']} at ~${entry:.3f}\n"
                        f"URL: {url}")
            )
            new_alerts += 1

        for k in exited_keys:
            prev_pos = state[wallet]["positions"].get(k, {})
            log(f"  ⚡ EXIT — {label}: {prev_pos.get('title','?')[:60]} — you should CLOSE your copy")
            notify(
                title=f"EXIT: {label} → close copy",
                body=f"{prev_pos.get('title','?')[:80]}",
                detail=(f"Wallet {label} ({wallet}) closed their position.\n"
                        f"Market: {prev_pos.get('title','?')}\n"
                        f"→ Sell your copy on Polymarket now.")
            )
            new_alerts += 1

        state[wallet] = {
            "positions": current,
            "last_poll": datetime.now().isoformat(timespec='seconds'),
        }
        time.sleep(1)  # be polite to data-api

    save_state(state)
    log(f"=== poll done, {new_alerts} alerts ===\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL)
    p.add_argument("--initialize", action="store_true",
                   help="fetch current positions without alerting (baseline for future polls)")
    a = p.parse_args()

    state = load_state()

    if a.initialize:
        log("=== INITIALIZE: fetching current positions without alerting ===")
        for label, wallet in WALLETS.items():
            positions = fetch_positions(wallet)
            current = {}
            for pos in positions:
                key = f"{pos.get('conditionId')}:{pos.get('asset')}"
                if float(pos.get("size") or 0) > 0:
                    current[key] = summarize_position(pos)
            state[wallet] = {
                "positions": current,
                "last_poll": datetime.now().isoformat(timespec='seconds'),
            }
            log(f"  [{label}] {wallet[:12]}: {len(current)} baseline positions")
            time.sleep(1)
        save_state(state)
        log("=== INITIALIZED. Next poll will only alert on CHANGES. ===")
        return

    if a.once:
        poll_once(state)
        return

    if a.daemon:
        log(f"=== COPY MONITOR DAEMON STARTED ===")
        log(f"Polling {len(WALLETS)} wallets every {a.interval}s")
        log(f"Bet size per copy: ${BET_SIZE}")
        log(f"Alerts logged to: {LOG}")
        while True:
            try: poll_once(state)
            except Exception as e: log(f"poll error: {e}")
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
