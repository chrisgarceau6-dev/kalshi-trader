#!/usr/bin/env python3
"""Polymarket US accessibility classifier + reward portfolio builder.

The polymarket.us API requires an API key we don't have yet (developer portal
is erroring). Instead, this uses YOUR tested data to predict which markets
in the full universe are likely accessible, and builds the reward portfolio.

CLASSIFIED FROM YOUR TESTS (2026-07-24):
  ACCESSIBLE:
    - Championship futures (NBA/NFL/EPL/UCL/MLB/WNBA/F1) — "will X win 2026/27 finals"
    - MLB divisional titles ("will X win 2026 AL Central")
    - LoL match markets ("LoL: X vs Y")
    - UK election winners (Clacton by-election winner)
    - F1 driver/constructor championships

  BLOCKED:
    - Cycling (Tour de France, all jerseys)
    - Player prop markets ("LeBron and Bronny to play together")
    - MLB Pennant races
    - Sub-outcome markets (vote counts, second place, "will there be no")
    - Non-Western elections (Brazil/Kazakhstan/Somaliland)
    - Counter-Strike / Valorant / Dota esports
    - LeBron team-destination props (specific rulings from CFTC)
    - EPL ownership props (Bezos Liverpool)

usage:
    python poly_us_classifier.py                    # build portfolio
    python poly_us_classifier.py --test "question"  # test one string
"""
import argparse, json, time, re
from datetime import datetime, timezone
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


# ---------------------------------------------------------------- classifier

def is_accessible(question):
    """Return (accessible: bool, confidence: float 0-1, reason: str)."""
    q = str(question or "").lower()

    # -- BLOCKED patterns (order matters — check specific rules first) --

    # Cycling — entire category
    if any(k in q for k in ["tour de france","vuelta","giro","yellow jersey",
                             "green jersey","polka dot","white jersey","peloton"]):
        return False, 0.95, "cycling (all blocked)"

    # Blocked esports
    if any(k in q for k in ["counter-strike","cs2:","cs:","csgo","valorant",
                             "val:","dota","dota2:"]):
        return False, 0.95, "blocked esports (CS/Valorant/Dota)"

    # Non-Western politics (Brazil/Central Asia/Africa/etc.)
    if any(k in q for k in ["pernambuco","kazakh","somaliland","brazilian",
                             "brazil","auyl","cook islands","zimbabwe",
                             "burkina","niger","mali","togo","cameroon",
                             "senegal","tunisia","yemen","syria","libya"]):
        return False, 0.85, "non-Western election (blocked)"

    # US presidential/state elections
    if any(k in q for k in ["2028 us presidential","2028 democratic",
                             "us presidential election","governor","senator",
                             "house seat","mayoral"]):
        return False, 0.75, "US election (mostly blocked)"

    # Player prop markets (specific players in scenarios)
    if re.search(r"(lebron|bronny).*(play together|to play)", q):
        return False, 0.90, "player prop (blocked)"

    # Sub-outcome markets (finishing 2nd, vote counts, etc.)
    if any(k in q for k in ["second place","finish in second","finish third",
                             "number of votes","vote count","turnout between",
                             "less than 10%","10-20%","60-70%","between",
                             "how many"]) and "champion" not in q:
        return False, 0.70, "sub-outcome market"

    # Negation markets
    if re.search(r"will there be no", q) or re.search(r"^no ", q):
        return False, 0.75, "negation market (usually blocked)"

    # MLB Pennant
    if "pennant" in q:
        return False, 0.90, "MLB pennant (blocked despite divisions working)"

    # Word bingo markets
    if any(k in q for k in ['warsh say','powell say','trump say','tweet',
                             'tweets from','post 40','post 60']):
        return False, 0.85, "word-bingo/count market"

    # Music/celebrity metrics
    if any(k in q for k in ["monthly listeners","spotify","album","song",
                             "streaming","netflix","emmys","oscars"]):
        return False, 0.85, "entertainment metric (blocked)"

    # Commodity/price markets
    if any(k in q for k in ["wti crude","natural gas","gold","silver","bitcoin",
                             "ethereum","eth ","btc ","hit (high)","hit (low)"]):
        return False, 0.80, "commodity/crypto price (blocked)"

    # Fed / macro / inflation
    if any(k in q for k in ["fed rate","fed interest","cpi","inflation",
                             "unemployment","jobs report","gdp"]):
        return False, 0.80, "macro/econ data (blocked)"

    # Ownership/business props
    if any(k in q for k in ["bezos","musk buys","zuckerberg buys",
                             "acquires","announced as new owner"]):
        return False, 0.75, "ownership prop (blocked)"

    # -- ACCESSIBLE patterns --

    # Sports championship futures — winning the top prize
    if re.search(r"win the 20(26|27|28).*(nba finals|super bowl|world series|"
                 r"premier league|champions league|nba champion|nfl champion|"
                 r"stanley cup|wnba finals|mls cup|epl|uefa|constructors|drivers)", q):
        return True, 0.95, "championship future (accessible)"

    # F1 championships
    if "f1" in q and ("champion" in q or "drivers'" in q or "constructors'" in q):
        return True, 0.90, "F1 championship (accessible)"

    # F1 grand prix winners
    if re.search(r"f1.*grand prix.*winner", q) or re.search(r"grand prix winner.*f1", q):
        return True, 0.85, "F1 grand prix winner"

    # MLB divisional titles
    if re.search(r"win the 2026.*(al|nl) (west|east|central) title", q):
        return True, 0.90, "MLB divisional title (accessible)"

    # LoL matches
    if q.startswith("lol:") or "league of legends" in q:
        return True, 0.85, "LoL match (accessible)"

    # UK/Western Europe elections — winner outcome
    if any(k in q for k in ["clacton","french presidential","german state",
                             "italian","spanish","dutch","polish","canadian"]):
        if "second place" not in q and "vote" not in q and "turnout" not in q:
            return True, 0.60, "UK/W-Europe election winner (usually accessible)"

    # WNBA finals
    if "wnba finals" in q:
        return True, 0.90, "WNBA finals (accessible)"

    # Default — unknown
    return None, 0.0, "unknown pattern"


# ---------------------------------------------------------------- data pull

def get_daily(cr):
    if not cr: return None
    if isinstance(cr, str):
        try: cr = json.loads(cr)
        except: return None
    if isinstance(cr, list):
        for c in cr:
            if isinstance(c, dict):
                v = c.get('rewardsDailyRate')
                if v:
                    try: return float(v)
                    except: pass
    return None


def dl(m):
    end = m.get('endDate') or ''
    try:
        d = datetime.fromisoformat(end.replace('Z','+00:00'))
        return (d - datetime.now(timezone.utc)).total_seconds() / 86400
    except: return 0


def clob_qual(token_id, mid, band):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id":token_id}, timeout=15)
        j = r.json()
        b = sum(float(x['size']) for x in (j.get('bids') or []) if float(x['price']) >= mid-band)
        a = sum(float(x['size']) for x in (j.get('asks') or []) if float(x['price']) <= mid+band)
        return b, a
    except:
        return 0, 0


def fetch_universe(pages=30):
    ms, offset = [], 0
    for _ in range(pages):
        r = requests.get(f"{GAMMA}/markets", params={"closed":"false","limit":100,"offset":offset,"order":"volume24hr","ascending":"false"}, timeout=30)
        b = r.json()
        if not b or not isinstance(b,list): break
        for m in b:
            if isinstance(m,dict): ms.append(m)
        offset += 100
        time.sleep(0.1)
    return ms


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", help="test one question string")
    p.add_argument("--pages", type=int, default=30)
    p.add_argument("--min-daily", type=float, default=15)
    p.add_argument("--min-dtr", type=float, default=1.0)
    p.add_argument("--min-conf", type=float, default=0.75, help="min accessibility confidence")
    p.add_argument("--check-depth", action="store_true", help="pull CLOB depth for share%")
    p.add_argument("--out", default="poly_us_portfolio.csv")
    a = p.parse_args()

    if a.test:
        result = is_accessible(a.test)
        print(f"Q: {a.test}")
        print(f"Accessible: {result[0]}  Confidence: {result[1]:.0%}  Reason: {result[2]}")
        return

    print(f"pulling up to {a.pages} pages of open markets...")
    markets = fetch_universe(a.pages)
    print(f"  {len(markets)} markets")

    rows = []
    for m in markets:
        q = m.get('question') or ''
        dr = get_daily(m.get('clobRewards'))
        if not dr or dr < a.min_daily: continue
        d = dl(m)
        if d < a.min_dtr: continue
        acc, conf, reason = is_accessible(q)
        if acc is not True: continue
        if conf < a.min_conf: continue
        try:
            bid = float(m.get('bestBid') or 0)
            ask = float(m.get('bestAsk') or 0)
        except: continue
        if not bid or not ask: continue
        ms = m.get('rewardsMinSize'); msp = m.get('rewardsMaxSpread')
        if not ms or not msp: continue
        cap = float(ms)*bid + float(ms)*(1-ask)
        row = dict(question=str(q)[:55], daily=dr, min_size=int(float(ms)),
                   mid=round((bid+ask)/2,3), dtr=round(d,1), cap=round(cap,2),
                   reason=reason)

        if a.check_depth:
            toks = m.get('clobTokenIds')
            if isinstance(toks,str):
                try: toks = json.loads(toks)
                except: toks = []
            if toks:
                mid = (bid+ask)/2
                band = float(msp)/100 if float(msp)>1 else float(msp)
                bq, aq = clob_qual(toks[0], mid, band)
                tot = (bq+aq)/2
                share = float(ms)/(float(ms)+tot) if tot > 0 else 0.3
                row['share_pct'] = round(share*100,2)
                row['exp_daily'] = round(dr*share,2)
                row['exp_weekly'] = round(dr*share*7,2)
                time.sleep(0.03)
        rows.append(row)

    if a.check_depth:
        rows.sort(key=lambda x:-x.get('exp_weekly',0))
    else:
        rows.sort(key=lambda x:-x['daily'])

    print(f"\nPredicted accessible reward markets: {len(rows)}\n")
    if a.check_depth:
        print(f"{'question':<57} {'daily':>7} {'min':>4} {'mid':>5} {'dtr':>5} {'cap':>7} {'share':>6} {'exp$/d':>7} {'exp$/wk':>8}  reason")
        for r in rows[:40]:
            print(f"{r['question']:<57} ${r['daily']:>6.0f} {r['min_size']:>4} {r['mid']:>5.2f} {r['dtr']:>5.1f} ${r['cap']:>6.2f} {r['share_pct']:>5.1f}% ${r['exp_daily']:>5.2f} ${r['exp_weekly']:>6.2f}  {r['reason']}")
        tot_cap = sum(r['cap'] for r in rows)
        tot_exp = sum(r.get('exp_weekly',0) for r in rows)
        print(f"\nFULL PORTFOLIO: {len(rows)} markets, cap=${tot_cap:.0f}, weekly gross=${tot_exp:.0f}")
        print(f"After 40% haircut: ${tot_exp*0.6:.0f}/week")
    else:
        print(f"{'question':<57} {'daily':>7} {'min':>4} {'mid':>5} {'dtr':>5} {'cap':>7}  reason")
        for r in rows[:60]:
            print(f"{r['question']:<57} ${r['daily']:>6.0f} {r['min_size']:>4} {r['mid']:>5.2f} {r['dtr']:>5.1f} ${r['cap']:>6.2f}  {r['reason']}")

    import csv
    if rows:
        with open(a.out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nsaved -> {a.out}")


if __name__ == "__main__":
    main()
