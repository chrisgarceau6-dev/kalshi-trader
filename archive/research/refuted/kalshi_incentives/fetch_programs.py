#!/usr/bin/env python3
"""Pull every incentive program Kalshi currently publishes (public endpoint, no auth).

    python3 fetch_programs.py            # writes programs_all.json + prints a summary
"""
import json, sys, time, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"
OUT = Path(__file__).resolve().parent / "programs_all.json"


def get(path, params=""):
    url = f"{API}/{path}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "incentive-research/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:
            if attempt == 3:
                print(f"  fetch failed: {exc}", file=sys.stderr)
                return {}
            time.sleep(1.5 * (attempt + 1))
    return {}


def fetch_all(limit=200, max_pages=200):
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        params = f"limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        r = get("incentive_programs", params)
        batch = r.get("incentive_programs", [])
        if not batch:
            break
        out.extend(batch)
        pages += 1
        cursor = r.get("next_cursor")
        if not cursor:
            break
        time.sleep(0.1)
    return out


if __name__ == "__main__":
    progs = fetch_all()
    OUT.write_text(json.dumps(progs, indent=1))
    print(f"{len(progs):,} programs -> {OUT.name}\n")

    print("incentive_type      :", dict(Counter(p.get("incentive_type") for p in progs)))
    print("incentive_description:", dict(Counter(p.get("incentive_description") for p in progs)))
    print("period_reward values :", dict(Counter(p.get("period_reward") for p in progs)))
    print("target_size_fp values:", dict(Counter(p.get("target_size_fp") for p in progs)))
    print("discount_factor_bps  :", dict(Counter(p.get("discount_factor_bps") for p in progs)))
    print("paid_out             :", dict(Counter(p.get("paid_out") for p in progs)))

    series = Counter(p["market_ticker"].split("-")[0] for p in progs if p.get("market_ticker"))
    print(f"\nseries covered ({len(series)}):")
    for s, n in series.most_common():
        print(f"  {s:<22} {n:>5} programs")

    spans = Counter()
    for p in progs:
        try:
            a = datetime.fromisoformat(p["start_date"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(p["end_date"].replace("Z", "+00:00"))
            spans[int((b - a).total_seconds() / 60)] += 1
        except Exception:
            pass
    print("\nprogram window length (minutes):", dict(spans))

    times = sorted(p["start_date"] for p in progs if p.get("start_date"))
    if times:
        print(f"window covered: {times[0]} -> {max(p['end_date'] for p in progs)}")
    now = datetime.now(timezone.utc)
    live = [p for p in progs
            if p.get("start_date", "") <= now.isoformat() <= p.get("end_date", "")]
    print(f"live right now ({now:%Y-%m-%dT%H:%M:%SZ}): {len(live)}")
