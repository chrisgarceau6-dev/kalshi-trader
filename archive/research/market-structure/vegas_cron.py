#!/usr/bin/env python3
"""Pull Vegas consensus odds from The Odds API.

Prophet Exchange (and other P2P sites) mirror Vegas closely because retail
users see Vegas prices elsewhere and quote accordingly. If Kalshi diverges
significantly from Vegas consensus, Prophet likely does too → arb candidate.

SETUP (2 min):
  1. Sign up free at the-odds-api.com  (500 requests/month free)
  2. Copy your API key
  3. Set env var:
       export ODDS_API_KEY=<your-key>
  4. Add cron:
       */30 * * * * cd /Users/chrisgarceau/pm && ./venv/bin/python vegas_cron.py

This pulls once every 30 min = ~1440/month. Free tier is 500/month, so either:
  - Cron every 90 min (480/month) — good enough
  - Pay $25/month for Basic tier (30k requests) — every 5 min possible
"""
import argparse, json, os, time
from datetime import datetime
import requests

ODDS = "https://api.the-odds-api.com/v4"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vegas_snapshot.json")

SPORTS = {
    "MLB":   "baseball_mlb",
    "NFL":   "americanfootball_nfl",
    "NBA":   "basketball_nba",
    "WNBA":  "basketball_wnba",
    "NHL":   "icehockey_nhl",
    "MLS":   "soccer_usa_mls",
    "NCAAF": "americanfootball_ncaaf",
    "NCAAB": "basketball_ncaab",
    "UFC":   "mma_mixed_martial_arts",
}

BOOKS = ['draftkings','fanduel','betmgm','caesars','espnbet']


def american_to_prob(o):
    return -o/(-o+100) if o < 0 else 100/(o+100)


def novig_consensus(offers):
    """Given list of {american: X, book: Y} for each side, compute no-vig consensus."""
    if not offers: return None
    probs = [american_to_prob(o['price']) for o in offers if o.get('price') is not None]
    if not probs: return None
    return sum(probs) / len(probs)


def fetch_sport(sport_key):
    """Fetch H2H (moneyline) odds for one sport."""
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(f"{ODDS}/sports/{sport_key}/odds", params={
            "apiKey": key, "regions": "us", "markets": "h2h",
            "oddsFormat": "american", "bookmakers": ",".join(BOOKS)
        }, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_all():
    out = {}
    for label, sport_key in SPORTS.items():
        games = fetch_sport(sport_key)
        if not games: continue
        out[label] = []
        for g in games:
            teams = {}
            for bm in g.get('bookmakers') or []:
                for mk in bm.get('markets') or []:
                    if mk.get('key') != 'h2h': continue
                    for oc in mk.get('outcomes') or []:
                        name = oc.get('name')
                        price = oc.get('price')
                        if name and price is not None:
                            teams.setdefault(name, []).append({'book': bm.get('key'), 'price': price})
            # compute no-vig consensus per team
            team_probs = {name: novig_consensus(offers) for name, offers in teams.items()}
            # normalize so probabilities sum to 1
            if len(team_probs) == 2:
                tot = sum(p for p in team_probs.values() if p is not None)
                if tot > 0:
                    team_probs = {n: (p/tot if p is not None else None) for n, p in team_probs.items()}
            out[label].append({
                'commence_time': g.get('commence_time',''),
                'home': g.get('home_team',''),
                'away': g.get('away_team',''),
                'consensus': team_probs,
                'n_books': len(g.get('bookmakers') or []),
            })
        time.sleep(0.5)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--interval", type=int, default=1800)  # 30 min default
    a = p.parse_args()

    if not os.environ.get("ODDS_API_KEY"):
        print("ODDS_API_KEY not set. Sign up at the-odds-api.com (free) and:")
        print("  export ODDS_API_KEY=<your-key>")
        return

    def run_once():
        data = fetch_all()
        if data:
            with open(OUT, 'w') as f:
                json.dump({'fetched_at': datetime.now().isoformat(timespec='seconds'),
                           'sports': data}, f)
            n = sum(len(v) for v in data.values())
            print(f"[{datetime.now().isoformat(timespec='seconds')}] saved {n} games -> {OUT}")

    if a.daemon:
        while True:
            try: run_once()
            except Exception as e: print(f"error: {e}")
            time.sleep(a.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
