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
from poly_us_classifier import is_accessible
try:
    import kalshi_auto_trader as kalshi
    KALSHI_ENABLED = bool(os.environ.get("KALSHI_API_KEY_ID"))
except ImportError:
    kalshi = None
    KALSHI_ENABLED = False

BASE = Path(__file__).parent
STATE = BASE / "copy_state.json"
LOG = BASE / "copy_alerts.log"
DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

# Sharp wallets with verified Kalshi-matchable markets
WALLETS = {
    "0x3dfb":   "0x3dfb153c197d4c19d3b31c1ecd2c7b6860eeabaf",  # MLB sharp, 94.7% win, $5,899/wk — primary
    "sentrio":  "0xdb83e85ffd22faa4009273034770f96ffc5b1e50",  # 99.3% win, $7,710/wk — MLB/MLS only
}
# Per-wallet bet cap — 0x3dfb is proven, sentrio is secondary with unknown MLB-specific rate
WALLET_MAX_BET = {
    "0x3dfb":  int(os.environ.get("MAX_BET", "100")),
    "sentrio": 50,
}
# Minimum THEIR position size before we copy — filters mid-game noise bets
WALLET_MIN_THEIR_DOLLARS = {
    "0x3dfb":  100,   # smallest 0x3dfb bet is $50K, this is just a safety floor
    "sentrio": 500,   # sentrio makes tiny noise bets; only copy real conviction
}
COPY_RATIO = 1.0           # fraction of their dollar bet to copy
MAX_BET = int(os.environ.get("MAX_BET", "100"))  # global fallback
MIN_BET = 5                # skip if copy bet would be below this
POLL_INTERVAL = 300        # 5 min
SIZE_CHANGE_THRESHOLD = 0.15  # alert when position size moves >=15%
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # set in GitHub Secrets


def send_ntfy(title, body):
    """Push notification via ntfy.sh — instant phone alert, no account needed."""
    if not NTFY_TOPIC:
        return False
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent", "Tags": "bell"},
            timeout=10,
        )
        return True
    except Exception as e:
        print(f"ntfy send failed: {e}", flush=True)
        return False


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
    """Send ntfy push + email. Falls back to Mac notification if neither configured."""
    email_body = f"{body}\n\n{detail}" if detail else body
    sent = send_ntfy(title, email_body)
    sent = send_email(f"[Copy Trade] {title}", email_body) or sent
    if not sent:
        notify_mac(title, body)


def log(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    if sys.stdout.isatty():
        print(line, flush=True)


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
    """Return the parent event slug (works on polymarket.us). Falls back to market slug."""
    try:
        r = requests.get(f"{GAMMA}/markets", params={"condition_ids": condition_id, "limit": 1}, timeout=10)
        j = r.json()
        if isinstance(j, list) and j:
            events = j[0].get("events") or []
            if events and events[0].get("slug"):
                return events[0]["slug"]
            return j[0].get("slug", "")
        return ""
    except: return ""


def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except: pass
    return {"kalshi_positions": {}}


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
    """Check all wallets, alert on new/exited/resized positions, update state."""
    log(f"=== POLL @ {datetime.now().strftime('%H:%M:%S')} ===")
    if KALSHI_ENABLED and kalshi:
        kalshi.check_stop_losses(state.setdefault("kalshi_positions", {}), log_fn=log)
    new_alerts = 0
    for label, wallet in WALLETS.items():
        first_seen = wallet not in state  # never polled before → initialize silently
        prev_data = state.get(wallet, {}).get("positions", {})
        prev = set(prev_data.keys())
        positions = fetch_positions(wallet)
        current = {}
        for p in positions:
            key = f"{p.get('conditionId')}:{p.get('asset')}"
            if float(p.get("size") or 0) > 0:
                current[key] = summarize_position(p)

        if first_seen:
            log(f"  [{label}] {wallet[:12]}: first poll — baseline {len(current)} positions (no alerts)")
            state[wallet] = {
                "positions": current,
                "last_poll": datetime.now().isoformat(timespec='seconds'),
            }
            save_state(state)
            time.sleep(1)
            continue

        new_keys = set(current.keys()) - prev
        exited_keys = prev - set(current.keys())

        # Filter to US-accessible markets only
        def us_ok(key, data=current):
            title = data.get(key, {}).get("title", "") or prev_data.get(key, {}).get("title", "")
            accessible, confidence, _ = is_accessible(title)
            return accessible is not False or confidence < 0.70

        new_keys    = {k for k in new_keys    if us_ok(k)}
        exited_keys = {k for k in exited_keys if us_ok(k)}

        # Detect size changes (adds or partial closes) on existing positions
        size_changes = []
        for k in set(current.keys()) & prev:
            if not us_ok(k):
                continue
            prev_size = prev_data.get(k, {}).get("size", 0)
            curr_size = current[k].get("size", 0)
            if prev_size > 0:
                change_pct = (curr_size - prev_size) / prev_size
                if abs(change_pct) >= SIZE_CHANGE_THRESHOLD:
                    size_changes.append((k, prev_size, curr_size, change_pct))

        if new_keys or exited_keys or size_changes:
            log(f"  [{label}] {wallet[:12]}: {len(current)} open "
                f"({len(new_keys)} NEW, {len(exited_keys)} EXITED, {len(size_changes)} SIZE CHANGE)")

        for k in new_keys:
            pos = current[k]
            entry = pos["avgPrice"]
            their_dollars = round(pos["size"] * entry, 2)
            min_their = WALLET_MIN_THEIR_DOLLARS.get(label, 0)
            if their_dollars < min_their:
                log(f"  skip — {label} bet ${their_dollars:.0f} below min ${min_their:.0f}")
                continue
            wallet_cap = WALLET_MAX_BET.get(label, MAX_BET)
            your_bet = min(wallet_cap, round(their_dollars * COPY_RATIO, 2))
            if your_bet < MIN_BET:
                log(f"  skip tiny bet — {label}: ${their_dollars:.0f} position, copy would be ${your_bet:.0f}")
                continue
            your_shares = round(your_bet / entry, 1) if entry > 0 else 0
            slug = market_slug(pos["conditionId"])
            url = f"https://polymarket.us/events/{slug}" if slug else pos["conditionId"]
            alert = (f"\n{'='*70}\n"
                     f"🎯 NEW COPY TRADE — {label} ({wallet[:10]})\n"
                     f"Market: {pos['title']}\n"
                     f"Their side: {pos['outcome']} @ avg ${entry:.3f}\n"
                     f"Their size: {pos['size']:.0f} shares (${their_dollars:.0f})\n"
                     f"YOUR COPY: Buy {your_shares} shares of {pos['outcome']} at ~${entry:.3f} (~${your_bet:.0f})\n"
                     f"URL: {url}\n"
                     f"{'='*70}")
            log(alert)
            kalshi_note = ""
            if KALSHI_ENABLED and kalshi and not kalshi.is_kill_switch_active(state, log_fn=log):
                kp = state.setdefault("kalshi_positions", {})
                placed = kalshi.execute_trade(
                    pos["title"], pos["outcome"], entry, their_dollars,
                    pos["conditionId"], kp, log_fn=log, max_bet=wallet_cap,
                )
                kalshi_note = "\nKalshi: AUTO-TRADED" if placed else "\nKalshi: no matching market"
            notify(
                title=f"NEW: {label} → {pos['outcome']} @ ${entry:.3f}",
                body=f"Buy {your_shares} shares for ${your_bet:.0f}\n{pos['title']}",
                detail=(f"Market: {pos['title']}\n"
                        f"Wallet: {label} ({wallet})\n"
                        f"Their side: {pos['outcome']} @ ${entry:.3f}\n"
                        f"Their size: {pos['size']:.0f} shares (~${their_dollars:.0f})\n\n"
                        f"YOUR COPY: Buy {your_shares} shares of {pos['outcome']} at ~${your_bet:.0f}\n"
                        f"URL: {url}{kalshi_note}")
            )
            new_alerts += 1

        for k in exited_keys:
            prev_pos = prev_data.get(k, {})
            log(f"  EXIT — {label}: {prev_pos.get('title','?')[:60]} — you should CLOSE your copy")
            condition_id = prev_pos.get("conditionId", k.split(":")[0])
            kalshi_note = ""
            if KALSHI_ENABLED and kalshi:
                kp = state.setdefault("kalshi_positions", {})
                closed = kalshi.close_trade(condition_id, kp, log_fn=log)
                kalshi_note = "\nKalshi: AUTO-CLOSED" if closed else ""
            notify(
                title=f"EXIT: {label} → close copy",
                body=f"{prev_pos.get('title','?')[:80]}",
                detail=(f"Wallet {label} ({wallet}) closed their position.\n"
                        f"Market: {prev_pos.get('title','?')}\n"
                        f"→ Sell your copy on Polymarket now.{kalshi_note}")
            )
            new_alerts += 1

        for k, prev_size, curr_size, change_pct in size_changes:
            pos = current[k]
            direction = "ADDED TO" if change_pct > 0 else "PARTIAL EXIT"
            entry = pos["avgPrice"]
            slug = market_slug(pos["conditionId"])
            url = f"https://polymarket.us/events/{slug}" if slug else pos["conditionId"]
            log(f"  {direction} — {label}: {pos['title'][:60]} "
                f"size {prev_size:.0f} → {curr_size:.0f} ({change_pct:+.0%})")
            notify(
                title=f"{direction}: {label} ({change_pct:+.0%})",
                body=f"{pos['title'][:80]}\n{pos['outcome']} {prev_size:.0f} → {curr_size:.0f} shares",
                detail=(f"Wallet {label} ({wallet})\n"
                        f"Market: {pos['title']}\n"
                        f"Side: {pos['outcome']} @ ${entry:.3f}\n"
                        f"Size: {prev_size:.0f} → {curr_size:.0f} shares ({change_pct:+.0%})\n"
                        f"URL: {url}")
            )
            new_alerts += 1

        state[wallet] = {
            "positions": current,
            "last_poll": datetime.now().isoformat(timespec='seconds'),
        }
        save_state(state)  # save per-wallet so a crash mid-poll doesn't lose state
        time.sleep(1)

    log(f"=== poll done, {new_alerts} alerts ===\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL)
    p.add_argument("--initialize", action="store_true",
                   help="fetch current positions without alerting (baseline for future polls)")
    p.add_argument("--test-email", action="store_true",
                   help="send a test email to verify SMTP credentials work, then exit")
    a = p.parse_args()

    if a.test_email:
        log("=== TEST NOTIFICATION ===")
        msg = ("Copy monitor is live. You will get this notification every time "
               "a tracked wallet opens, closes, adds to, or partially exits a position.\n\n"
               f"Wallets: {list(WALLETS.keys())}\n"
               f"Poll interval: {POLL_INTERVAL}s")
        ntfy_ok = send_ntfy("[Copy Monitor] Test", msg)
        email_ok = send_email("[Copy Monitor] Test", msg)
        if ntfy_ok:
            log("ntfy push sent successfully.")
        else:
            log("ntfy skipped (NTFY_TOPIC not set or failed).")
        if email_ok:
            log("Test email sent successfully.")
        else:
            log("Email failed or not configured.")
        if not ntfy_ok and not email_ok:
            log("ERROR: no notification method worked.")
            sys.exit(1)
        return

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
        log(f"Bet size per copy: up to ${MAX_BET}")
        log(f"Alerts logged to: {LOG}")
        while True:
            try: poll_once(state)
            except Exception as e: log(f"poll error: {e}")
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
