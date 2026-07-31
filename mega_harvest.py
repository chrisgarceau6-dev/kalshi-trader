#!/usr/bin/env python3
"""Mega-harvest: pull wallets from EVERY angle we haven't hit yet.

Sources:
  1. Data-api leaderboard: OVERALL / POLITICS / SPORTS / CRYPTO / WEATHER, ALL and MONTH windows
  2. Top holders on largest current open markets
  3. Top wallets on recently-resolved high-volume markets
  4. Cross-referenced with existing wallets to exclude dups

Screens all of them, writes new candidates to mega_harvest_hits.csv.
"""
import argparse, json, os, subprocess, sys, time, re
from datetime import datetime
from pathlib import Path
import requests
import pandas as pd

BASE = Path(__file__).parent
DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
PROGRESS = BASE / "mega_harvest_progress.txt"
CANDIDATES = BASE / "mega_harvest_candidates.txt"
RESULTS = BASE / "mega_harvest_hits.csv"


def log(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}\n"
    with open(PROGRESS, "a") as f: f.write(line)
    print(line, end="", flush=True)


def load_existing_wallets():
    """Everything we've already tested."""
    known = set()
    for f in ["all_wallets_master.txt", "hunt_new_candidates.txt", "hunt_harvested.txt"]:
        p = BASE / f
        if p.exists():
            with open(p) as fh:
                for line in fh:
                    line = line.strip().lower()
                    if line.startswith("0x"):
                        known.add(line)
    return known


def leaderboard(category, time_period, top_n=100):
    """Pull top wallets from a specific category + time window."""
    rows = []
    offset = 0
    while offset < top_n:
        try:
            r = requests.get(f"{DATA}/v1/leaderboard", params={
                "category": category, "timePeriod": time_period,
                "orderBy": "PNL", "limit": 50, "offset": offset
            }, timeout=20)
            if r.status_code != 200: break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch: break
            for row in batch:
                w = (row.get("proxyWallet") or row.get("wallet") or "").lower()
                if w.startswith("0x"):
                    rows.append(w)
            if len(batch) < 50: break
            offset += 50
            time.sleep(0.2)
        except: break
    return rows


def market_holders(cid, top_n=30):
    """Pull top position holders for a specific market."""
    try:
        r = requests.get(f"{DATA}/positions", params={
            "condition_id": cid, "limit": top_n, "sortBy": "size", "order": "desc"
        }, timeout=20)
        if r.status_code != 200: return []
        rows = r.json() or []
        return [(row.get("proxyWallet") or "").lower() for row in rows if isinstance(row, dict)]
    except: return []


def top_current_markets(n=30):
    """Highest-volume currently-open markets."""
    try:
        r = requests.get(f"{GAMMA}/markets", params={
            "closed": "false", "limit": n, "order": "volumeNum", "ascending": "false"
        }, timeout=30)
        return [m for m in (r.json() or []) if isinstance(m, dict)]
    except: return []


def top_resolved_markets(days=90, n=50):
    from datetime import datetime as dt, timedelta, timezone
    since = (dt.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        r = requests.get(f"{GAMMA}/markets", params={
            "closed": "true", "limit": n, "order": "volumeNum", "ascending": "false",
            "end_date_min": since
        }, timeout=30)
        return [m for m in (r.json() or []) if isinstance(m, dict)]
    except: return []


def screen(wallet):
    try:
        r = subprocess.run(
            ["python", str(BASE / "wallet_5point2.py"), wallet],
            capture_output=True, text=True, timeout=180, cwd=str(BASE)
        )
    except: return None
    out = r.stdout
    def grab(pat, cast=str, default=None):
        m = re.search(pat, out)
        try: return cast(m.group(1)) if m else default
        except: return default
    def hard(name): return f"{name} [HARD]: PASS" in out
    return {
        "wallet": wallet,
        "n": grab(r"corrected: n=(\d+)", int, 0),
        "pnl_c": grab(r"corrected: n=\d+\s+pnl=\$(-?[\d,]+)",
                      lambda s: float(s.replace(",", "")), 0),
        "t_stat": grab(r"t_stat=([-\d.]+)", float, 0),
        "months": grab(r"months=(\d+)", int, 0),
        "top_share": grab(r"top_month_share=([\d.]+)", float, 0),
        "median_hold": grab(r"median_hold=([\d.]+)h", float, 0),
        "h1_edge": hard("1_edge_vs_spread"),
        "h3_sig":  hard("3_significant_edge_p05"),
        "h4_samp": hard("4_sample_and_spread"),
    }


def main():
    with open(PROGRESS, "w") as f:
        f.write(f"=== mega-harvest {datetime.now().isoformat(timespec='seconds')} ===\n")

    known = load_existing_wallets()
    log(f"already tested: {len(known)} wallets")

    all_new = set()

    # Source 1: leaderboard × category × window
    log("=== SOURCE 1: leaderboard sweeps ===")
    for cat in ["OVERALL","POLITICS","SPORTS","CRYPTO","WEATHER","ECONOMICS","CULTURE"]:
        for tp in ["ALL","MONTH","WEEK"]:
            hits = leaderboard(cat, tp, top_n=150)
            new = [w for w in hits if w not in known and w not in all_new]
            all_new.update(new)
            log(f"  {cat}/{tp}: {len(hits)} pulled, {len(new)} new")

    log(f"after leaderboard: {len(all_new)} unique new candidates")

    # Source 2: top holders on hottest current markets
    log("=== SOURCE 2: top holders in hottest current markets ===")
    markets = top_current_markets(50)
    for m in markets[:30]:
        cid = m.get("conditionId")
        if not cid: continue
        holders = market_holders(cid, top_n=30)
        new = [w for w in holders if w and w not in known and w not in all_new]
        all_new.update(new)
    log(f"after current market holders: {len(all_new)} new")

    # Source 3: top holders on recently resolved big markets
    log("=== SOURCE 3: winners on recently resolved markets ===")
    resolved = top_resolved_markets(days=90, n=80)
    for m in resolved[:50]:
        cid = m.get("conditionId")
        if not cid: continue
        holders = market_holders(cid, top_n=20)
        new = [w for w in holders if w and w not in known and w not in all_new]
        all_new.update(new)
    log(f"after resolved market winners: {len(all_new)} new")

    # Write candidates file
    all_new = list(all_new)
    with open(CANDIDATES, "w") as f:
        f.write("\n".join(all_new) + "\n")
    log(f"=== HARVESTED {len(all_new)} NEW CANDIDATES ===")
    log(f"candidates saved -> {CANDIDATES}")

    # Screen each
    log(f"=== SCREENING {len(all_new)} candidates ===")
    hits = []
    for i, w in enumerate(all_new, 1):
        row = screen(w)
        if row is None:
            continue
        hard_score = sum([row["h1_edge"], row["h3_sig"], row["h4_samp"]])
        row["hard_score"] = hard_score
        if hard_score >= 2:
            hits.append(row)
        if i % 25 == 0 or hard_score >= 2:
            log(f"  {i}/{len(all_new)} screened. latest hits: {len(hits)}, latest={w[:10]} hard={hard_score} t={row.get('t_stat',0)}")

    if hits:
        pd.DataFrame(hits).to_csv(RESULTS, index=False)
        log(f"=== DONE: {len(hits)} new 2/3+ HARD hits ===")
        log(f"saved -> {RESULTS}")
        # Print top 10
        h = pd.DataFrame(hits).sort_values(["hard_score","t_stat"], ascending=[False, False])
        for _, r in h.head(15).iterrows():
            log(f"  {r['wallet']} hard={r['hard_score']} t={r['t_stat']:.1f} n={r['n']} pnl=${r['pnl_c']:,.0f} months={r['months']}")
    else:
        log("no new hits from mega-harvest")

    log("=== MEGA HARVEST COMPLETE ===")


if __name__ == "__main__":
    main()
