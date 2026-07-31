#!/usr/bin/env python3
"""Cron job: fetch Kalshi sports games every 5 min, save to kalshi_snapshot.json.

Usage (via cron):
    */5 * * * * cd /Users/chrisgarceau/pm && ./venv/bin/python arb_cron.py

Or launch as a daemon loop:
    python arb_cron.py --daemon
"""
import argparse, json, re, time, os
from datetime import datetime, timezone
import requests

_MON = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
        'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}

def game_time_from_ticker(ticker):
    """Parse game start time from ticker like KXMLBGAME-26JUL251910HOUCWS-HOU
    → year 2026, JUL 25, 19:10 UTC."""
    m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{4})', ticker or '')
    if not m: return None
    yy, mon, dd, hhmm = m.groups()
    if mon not in _MON: return None
    try:
        return datetime(2000+int(yy), _MON[mon], int(dd),
                        int(hhmm[:2]), int(hhmm[2:]), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "chris-arb/0.1"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalshi_snapshot.json")


def live_book_top(ticker):
    """Pull actual live orderbook — this is the real bid/ask, not summary lag."""
    try:
        r = requests.get(f"{KALSHI}/markets/{ticker}/orderbook", headers=UA, timeout=10)
        j = r.json()
        ob = j.get('orderbook', j)
        # Kalshi book: 'yes' = list of [price_cents, size] resting BID on YES tokens
        #              'no'  = list of [price_cents, size] resting BID on NO tokens
        # Best YES ASK = 1 - (best NO bid) — because someone selling YES = someone buying NO
        # Best YES BID = highest resting bid on yes list
        yes_bids = ob.get('yes') or []
        no_bids  = ob.get('no')  or []
        best_yes_bid = max((float(b[0])/100 for b in yes_bids), default=None)
        best_no_bid  = max((float(b[0])/100 for b in no_bids),  default=None)
        best_yes_ask = (1.0 - best_no_bid) if best_no_bid is not None else None
        return best_yes_bid, best_yes_ask
    except Exception:
        return None, None


def fv(m, *ks):
    for k in ks:
        v = m.get(k)
        if v not in (None,"","0.0000"):
            try:
                x = float(v)
                return x/100 if x>1.5 else x
            except: pass
    return None


def fetch_all():
    games = []
    series_map = {"MLB":"KXMLBGAME","NFL":"KXNFLGAME","NBA":"KXNBAGAME","WNBA":"KXWNBAGAME",
                  "NHL":"KXNHLGAME","MLS":"KXMLSGAME","NCAAF":"KXNCAAFGAME",
                  "NCAAB":"KXNCAABGAME","UFC":"KXUFCFIGHT"}
    for sport, stk in series_map.items():
        try:
            r = requests.get(f"{KALSHI}/markets", params={"series_ticker":stk,"status":"open","limit":100},
                             headers=UA, timeout=15)
            ms = r.json().get('markets') or []
        except Exception:
            continue
        events = {}
        for m in ms:
            ev = m.get('event_ticker','')
            if ev not in events: events[ev] = []
            game_time = game_time_from_ticker(m.get('ticker',''))
            events[ev].append({
                'ticker':m.get('ticker'),
                'team':m.get('yes_sub_title','')[:22],
                'close':(game_time or m.get('close_time',''))[:16],
                'market_close':m.get('close_time','')[:16],
                'game_time': game_time,
                # Keep the summary values as fallback / reference
                'summary_bid': fv(m, 'yes_bid_dollars', 'yes_bid'),
                'summary_ask': fv(m, 'yes_ask_dollars', 'yes_ask'),
            })
        # For each event, pull LIVE ORDERBOOK for accuracy
        for ev, sides in events.items():
            live_count = 0
            for s in sides:
                yb, ya = live_book_top(s['ticker'])
                if yb is not None and ya is not None:
                    s['yes_bid'] = yb
                    s['yes_ask'] = ya
                    s['live_book'] = True
                    live_count += 1
                else:
                    # Fall back to summary but mark it
                    s['yes_bid'] = s.pop('summary_bid', None)
                    s['yes_ask'] = s.pop('summary_ask', None)
                    s['live_book'] = False
                time.sleep(0.08)
            valid = [s for s in sides if s.get('yes_bid') is not None and s.get('yes_ask') is not None]
            if len(valid) >= 2:
                spread = sum(s['yes_ask']-s['yes_bid'] for s in valid) / len(valid)
                games.append({'sport':sport,'event':ev,'sides':valid,
                              'close':valid[0].get('close',''),'avg_spread':round(spread,4),
                              'live_book_count': live_count, 'total_sides': len(valid)})
        time.sleep(0.15)
    games.sort(key=lambda g:-g['avg_spread'])
    return games


def save(games):
    with open(OUT, 'w') as f:
        json.dump({'fetched_at': datetime.now().isoformat(timespec='seconds'),
                   'games': games}, f)
    print(f"[{datetime.now().isoformat(timespec='seconds')}] saved {len(games)} games -> {OUT}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true", help="loop every 5 minutes")
    p.add_argument("--interval", type=int, default=300)
    a = p.parse_args()

    if a.daemon:
        while True:
            try:
                games = fetch_all()
                save(games)
            except Exception as e:
                print(f"error: {e}")
            time.sleep(a.interval)
    else:
        games = fetch_all()
        save(games)


if __name__ == "__main__":
    main()
