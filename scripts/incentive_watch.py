#!/usr/bin/env python3
"""Is there a Kalshi incentive pool on a series we trade — and is it one we can collect?

Kalshi runs two self-serve reward programs a retail algo account is eligible for.
The Market Maker tier needs a signed agreement we do not have and is excluded.

  VOLUME    pays pro-rata on MATCHED VOLUME, counts BOTH sides, so our taker flow
            qualifies with NO strategy change. Capped $0.005/contract/account.
            Eligible volume is central-order-book fills priced 3c-97c, and our
            90-93c band sits inside it. THIS IS THE ONLY ONE WE CAN COLLECT.
  LIQUIDITY pays for RESTING SIZE near a reference price, scored on 1-second
            snapshots. We are 100% taker, so we earn exactly $0 no matter how many
            run on our series. Printed for context; NEVER alerted on. Alerting on
            money we structurally cannot collect is how an alert channel dies.

Measured 2026-09-01: KXBTC15M and KXETH15M each ran 306 VOLUME pools at $20/market,
but only 2026-05-09 -> 2026-05-12. A ~3-day pilot, long over. We would have qualified
automatically and nobody was watching. Nothing live on our six since. Ceiling if one
returns across all six: ~$0.005 x ~4k contracts/day = ~$20/day, ~$600/mo, against a
fee spend of ~$650/mo. Worth catching automatically; not worth restructuring for.

    python3 scripts/incentive_watch.py             # live check + historical context
    python3 scripts/incentive_watch.py --email     # CI: silent unless collectable
"""
import argparse, collections, datetime as D, json, os, re, sys, urllib.request

BASE = "https://external-api.kalshi.com/trade-api/v2/incentive_programs"
# Keep in sync with LC_SERIES in scripts/kstat.py.
LC = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
PAGE = 1000


def pt(x):
    """Kalshi returns variable sub-second precision; pad to 6 digits (as _pts does)."""
    x = (x or "").replace("Z", "+00:00")
    x = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), x)
    return D.datetime.fromisoformat(x)


def pull(status, kind):
    """Returns (programs, truncated). The API caps a page at PAGE and has returned an
    EMPTY cursor while more rows existed, so a full page with no cursor is treated as
    possible truncation rather than a clean end — the #233 lesson, applied to a
    different feed. Callers must not report a zero drawn from a truncated list."""
    out, cursor, pages = [], "", 0
    while True:
        url = f"{BASE}?status={status}&type={kind}&limit={PAGE}"
        if cursor:
            url += f"&cursor={cursor}"
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
        batch = d.get("incentive_programs") or []
        for p in batch:
            p["_s"] = re.split(r"-", p.get("market_ticker") or "")[0]
        out += batch
        cursor = d.get("cursor") or ""
        pages += 1
        if not cursor or not batch or pages > 40:
            return out, ((not cursor) and len(batch) == PAGE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", action="store_true", help="email only if collectable")
    ap.add_argument("--all", action="store_true", help="list every covered series")
    a = ap.parse_args()
    now = D.datetime.now(D.timezone.utc)
    lines, actionable, blind = [], [], []

    # ── The alert path. status=active&type=volume is the ONLY query this decision
    #    rests on, and it is deliberately the narrow one: it returns a couple dozen
    #    rows, well under the page cap, so a zero from it is PROVEN. The broad
    #    status=all sweep below truncates at 1000 and can bury our series entirely —
    #    a first pass at this script built the alert on it and reported a false zero.
    act, act_trunc = pull("active", "volume")
    mine_live = [p for p in act
                 if p["_s"] in LC and pt(p["start_date"]) <= now < pt(p["end_date"])]
    lines.append(f"VOLUME   active: {len(act)} programs across "
                 f"{len({p['_s'] for p in act})} series")
    if act_trunc:
        blind.append("the active-volume list hit the page cap with no cursor, so a "
                     "zero could not be proven this run")
    for s in LC:
        r = [p for p in mine_live if p["_s"] == s]
        if r:
            pool = sum(p.get("period_reward", 0) for p in r) / 10000
            ends = max(pt(p["end_date"]) for p in r)
            actionable.append(f"{s}: {len(r)} live pool(s), ${pool:,.2f}, "
                              f"ends {ends.isoformat()[:16]}Z")
    lines.append(f"  on our six: {len(mine_live)} LIVE"
                 + ("" if mine_live else "  — nothing to collect"))

    # ── Context only. Never drives the alert.
    liq, _ = pull("active", "liquidity")
    liq_mine = [p for p in liq if p["_s"] in LC]
    lines.append(f"LIQUIDITY active: {len(liq)} programs, {len(liq_mine)} on our six "
                 f"(we are 100% taker — worth $0 to us either way)")

    hist, _ = pull("all", "volume")
    seen = collections.Counter(p["_s"] for p in hist if p["_s"] in LC)
    if seen:
        lines.append("historical VOLUME pools on our six (list truncates at "
                     f"{PAGE}, so counts are a floor):")
        for s, n in seen.most_common():
            r = [p for p in hist if p["_s"] == s]
            lines.append(f"    {s:12s} {n:4d} programs  "
                         f"${sum(p.get('period_reward', 0) for p in r)/10000:>9,.2f}  "
                         f"{pt(min(p['start_date'] for p in r)).date()} -> "
                         f"{pt(max(p['end_date'] for p in r)).date()}")
    if a.all:
        for s, n in collections.Counter(p["_s"] for p in act + liq).most_common():
            lines.append(f"    {s:26s} {n:5d}")

    if actionable:
        lines += ["", "*** COLLECTABLE POOL ON A SERIES WE TRADE ***"] + \
                 ["    " + x for x in actionable] + \
                 ["", "    Taker fills qualify, so this pays with no strategy change.",
                  "    Confirm the side restriction on the market page, then just trade.",
                  "    Ceiling is $0.005/contract/account, ~$20/day at our run rate."]
    body = "\n".join(lines)
    print(body)
    _alert(a.email, actionable, blind, body)
    return 2 if actionable else 0


def _alert(want_email, actionable, blind, body):
    """Email ONLY when a pool we can actually collect is live, or when the narrow
    query that decides that could not be trusted. Never on liquidity pools and never
    on a quiet day: daily_summary.yml runs this with `|| true`, so email is the only
    live channel, and a channel that fires daily gets filtered — which is exactly the
    blindness #233 was about. A quiet run is the healthy state and must stay silent."""
    if not want_email or not (actionable or blind):
        return
    subject = ("[Kalshi] INCENTIVE POOL LIVE on a series we trade" if actionable
               else "[Kalshi] incentive watch could not prove a zero")
    try:
        import smtplib
        from email.mime.text import MIMEText
        # Secret names must match daily_summary.yml — COPY_EMAIL_*, not GMAIL_*.
        frm = os.environ.get("COPY_EMAIL_FROM", "")
        to  = os.environ.get("COPY_EMAIL_TO", "")
        pwd = os.environ.get("COPY_EMAIL_PASSWORD", "")
        if frm and to and pwd:
            msg = MIMEText(body + ("\n\nBLIND: " + "; ".join(blind) if blind else ""))
            msg["Subject"] = subject
            msg["From"], msg["To"] = frm, to
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as sv:
                sv.login(frm, pwd); sv.send_message(msg)
            print("\n  alert emailed")
        else:
            print("\n  email not configured (COPY_EMAIL_FROM/TO/PASSWORD)")
    except Exception as exc:
        print(f"\n  email failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
