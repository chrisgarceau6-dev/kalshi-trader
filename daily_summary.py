#!/usr/bin/env python3
"""Daily P&L summary — pulls ground-truth data directly from Kalshi API.

Covers the 24h window ending at script runtime (run at 10pm ET nightly).
Stats are accurate — not derived from local state which resets on cache loss.

Run manually: python3 daily_summary.py
GitHub Actions: triggered by daily_summary.yml at 02:00 UTC (10pm ET)
"""

import os, smtplib, time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from kalshi_auth import get as kalshi_get

SERIES_LIST = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]


def fetch_balance():
    try:
        code, r = kalshi_get("/portfolio/balance")
        if code == 200:
            bal = r.get("balance", 0)
            return bal / 100.0 if isinstance(bal, int) and bal > 1000 else float(bal)
    except Exception:
        pass
    return None


def fetch_settlements(min_ts, max_ts):
    results, cursor = [], None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            code, r = kalshi_get("/portfolio/settlements", params)
        except Exception:
            time.sleep(2)
            continue
        if code != 200:
            break
        batch = r.get("settlements", [])
        if not batch:
            break
        stopped = False
        for s in batch:
            ts = s.get("settled_time", "")
            if ts:
                try:
                    t = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    t = 0
                if t < min_ts:
                    stopped = True
                    break
                if t <= max_ts:
                    results.append(s)
        cursor = r.get("cursor")
        if stopped or not cursor:
            break
        time.sleep(0.05)
    return results


def send_email(subject, body):
    to_addr   = os.environ.get("COPY_EMAIL_TO", "")
    from_addr = os.environ.get("COPY_EMAIL_FROM", "")
    password  = os.environ.get("COPY_EMAIL_PASSWORD", "")
    if not all([to_addr, from_addr, password]):
        print("Email env vars missing — printing to stdout instead.")
        print(f"\n{'='*60}\n{subject}\n{'='*60}\n{body}")
        return
    try:
        msg = MIMEText(body, "plain")
        msg["From"], msg["To"], msg["Subject"] = from_addr, to_addr, subject
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(from_addr, password)
            s.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")
        print(f"\n{subject}\n{body}")


def series_from_ticker(ticker):
    parts = ticker.split("-")
    return parts[0] if parts else ticker


def main():
    now    = datetime.now(timezone.utc)
    max_ts = int(now.timestamp())
    min_ts = max_ts - 86400

    window_start = datetime.fromtimestamp(min_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    window_end   = now.strftime("%Y-%m-%d %H:%M UTC")
    print(f"Daily summary: {window_start} → {window_end}")

    balance = fetch_balance()
    print(f"Balance: ${balance:.2f}" if balance else "Balance: unavailable")

    print("Fetching settlements...")
    settlements = fetch_settlements(min_ts, max_ts)
    print(f"Found {len(settlements)} settlements")

    if not settlements:
        body = (f"Period:  {window_start}  →  {window_end}\n"
                f"Balance: ${balance:.2f}\n\n"
                f"No trades settled in this period.")
        send_email("[Kalshi] Daily Summary — no trades", body)
        return

    trades = []
    for s in settlements:
        ticker = s.get("ticker", "")

        revenue_raw = s.get("revenue", 0)
        revenue = revenue_raw / 100.0 if isinstance(revenue_raw, int) and revenue_raw > 100 else float(revenue_raw or 0)

        yes_cost = float(s.get("yes_total_cost_dollars", 0) or 0)
        no_cost  = float(s.get("no_total_cost_dollars",  0) or 0)

        if yes_cost > 0.01:
            side, cost = "yes", yes_cost
        elif no_cost > 0.01:
            side, cost = "no", no_cost
        else:
            side, cost = "?", 0.0

        won = revenue > 0.01
        pnl = revenue - cost if won else -cost

        trades.append({
            "ticker": ticker,
            "series": series_from_ticker(ticker),
            "side":   side,
            "cost":   cost,
            "revenue": revenue,
            "pnl":    pnl,
            "won":    won,
        })

    n          = len(trades)
    wins       = sum(1 for t in trades if t["won"])
    losses     = n - wins
    wr         = wins / n * 100 if n else 0
    gross_pnl  = sum(t["pnl"] for t in trades)
    wagered    = sum(t["cost"] for t in trades)
    avg_per    = gross_pnl / n if n else 0

    series_stats = {}
    for t in trades:
        s = t["series"]
        if s not in series_stats:
            series_stats[s] = {"n": 0, "wins": 0, "pnl": 0.0}
        series_stats[s]["n"]    += 1
        series_stats[s]["wins"] += int(t["won"])
        series_stats[s]["pnl"]  += t["pnl"]

    loss_trades = sorted([t for t in trades if not t["won"]], key=lambda t: t["pnl"])

    lines = [
        f"Period:    {window_start}  →  {window_end}",
        f"Balance:   ${balance:.2f}" if balance else "Balance:   unavailable",
        "",
        "── SUMMARY ──────────────────────────────",
        f"Trades:    {n}  ({wins}W / {losses}L)",
        f"Win rate:  {wr:.1f}%",
        f"Net P&L:   ${gross_pnl:+.2f}",
        f"$/trade:   ${avg_per:+.2f}",
        f"Wagered:   ${wagered:.2f}",
        "",
        "── BY SERIES ────────────────────────────",
    ]

    for series in SERIES_LIST:
        if series not in series_stats:
            continue
        ss  = series_stats[series]
        swr = ss["wins"] / ss["n"] * 100 if ss["n"] else 0
        lines.append(f"  {series:<14} {ss['n']:>3} trades  {swr:>5.1f}% WR  ${ss['pnl']:>+7.2f}")

    if loss_trades:
        lines += ["", "── LOSSES ───────────────────────────────"]
        for t in loss_trades:
            lines.append(f"  {t['ticker']}  {(t['side'] or '?').upper()}  cost=${t['cost']:.2f}  loss=${t['pnl']:.2f}")

    lines += ["", f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}"]

    body  = "\n".join(lines)
    sign  = "+" if gross_pnl >= 0 else "-"
    bal_s = f"  bal=${balance:.0f}" if balance else ""
    subj  = f"[Kalshi] Daily: {wins}/{n} = {wr:.1f}% WR  {sign}${abs(gross_pnl):.2f}{bal_s}"

    print(body)
    send_email(subj, body)


if __name__ == "__main__":
    main()
