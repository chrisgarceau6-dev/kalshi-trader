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
MAX_PAGES   = 20   # safety cap — 20 pages × 200 = 4,000 settlements max


def fetch_balance():
    try:
        code, r = kalshi_get("/portfolio/balance")
        if code == 200:
            return float(r.get("balance_dollars", 0) or 0)
    except Exception:
        pass
    return None


def fetch_settlements(min_ts, max_ts):
    """Fetch settlements in [min_ts, max_ts]. API returns newest-first."""
    results, cursor, pages = [], None, 0
    while pages < MAX_PAGES:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            code, r = kalshi_get("/portfolio/settlements", params)
        except Exception as e:
            print(f"  settlements fetch error: {e} — retrying once")
            time.sleep(3)
            try:
                code, r = kalshi_get("/portfolio/settlements", params)
            except Exception:
                break
        if code != 200:
            print(f"  settlements HTTP {code}")
            break
        batch = r.get("settlements", [])
        if not batch:
            break
        pages += 1
        stopped = False
        for s in batch:
            ts_str = s.get("settled_time", "")
            if ts_str:
                try:
                    t = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
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
        time.sleep(0.1)
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
    return ticker.split("-")[0]


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
        ticker    = s.get("ticker", "")
        revenue   = int(s.get("revenue", 0)) / 100.0
        yes_cost  = float(s.get("yes_total_cost_dollars", 0) or 0)
        no_cost   = float(s.get("no_total_cost_dollars",  0) or 0)
        fee       = float(s.get("fee_cost", 0) or 0)
        cost      = yes_cost + no_cost
        side      = "yes" if yes_cost > 0.001 else ("no" if no_cost > 0.001 else "?")
        contracts = float(s.get("yes_count_fp" if side == "yes" else "no_count_fp", 0) or 0)
        won       = revenue > 0.01
        pnl       = revenue - cost - fee

        trades.append({
            "ticker":    ticker,
            "series":    series_from_ticker(ticker),
            "side":      side,
            "contracts": contracts,
            "cost":      cost,
            "revenue":   revenue,
            "fee":       fee,
            "pnl":       pnl,
            "won":       won,
        })

    n         = len(trades)
    wins      = sum(1 for t in trades if t["won"])
    losses    = n - wins
    wr        = wins / n * 100 if n else 0
    gross_pnl = sum(t["pnl"] for t in trades)
    wagered   = sum(t["cost"] for t in trades)
    avg_per   = gross_pnl / n if n else 0
    avg_fill  = sum(t["contracts"] for t in trades) / n if n else 0

    series_stats = {}
    for t in trades:
        s = t["series"]
        if s not in series_stats:
            series_stats[s] = {"n": 0, "wins": 0, "pnl": 0.0}
        series_stats[s]["n"]    += 1
        series_stats[s]["wins"] += int(t["won"])
        series_stats[s]["pnl"]  += t["pnl"]

    loss_trades = sorted([t for t in trades if not t["won"]], key=lambda t: t["pnl"])
    partial_fills = [t for t in trades if t["contracts"] > 0 and t["contracts"] < 20]

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
        f"Avg fill:  {avg_fill:.1f} contracts",
    ]

    if partial_fills:
        lines.append(f"Partials:  {len(partial_fills)} trades with <20 contracts")

    lines += ["", "── BY SERIES ────────────────────────────"]
    for series in SERIES_LIST:
        if series not in series_stats:
            continue
        ss  = series_stats[series]
        swr = ss["wins"] / ss["n"] * 100 if ss["n"] else 0
        lines.append(f"  {series:<14} {ss['n']:>3} trades  {swr:>5.1f}% WR  ${ss['pnl']:>+7.2f}")

    if loss_trades:
        lines += ["", "── LOSSES ───────────────────────────────"]
        for t in loss_trades:
            lines.append(f"  {t['ticker']}  {t['side'].upper()}  {t['contracts']:.0f}ct  cost=${t['cost']:.2f}  loss=${t['pnl']:.2f}")

    lines += ["", f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}"]

    body  = "\n".join(lines)
    sign  = "+" if gross_pnl >= 0 else "-"
    bal_s = f"  bal=${balance:.0f}" if balance else ""
    subj  = f"[Kalshi] Daily: {wins}/{n} = {wr:.1f}% WR  {sign}${abs(gross_pnl):.2f}{bal_s}"

    print(body)
    send_email(subj, body)


if __name__ == "__main__":
    main()
