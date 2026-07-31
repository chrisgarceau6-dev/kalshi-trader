#!/usr/bin/env python3
"""Interactive terminal dashboard for multi-venue sports arb.

usage:
    python arb_dashboard.py
"""
import json, math, time, sys, csv, os
from datetime import datetime
import requests

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, FloatPrompt, IntPrompt
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich import box

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "chris-arb/0.1"}
LOG_FILE = "arb_log.csv"

console = Console()

FEES = {
    "kalshi":    lambda p: 0.07 * p * (1-p),
    "robinhood": lambda p: 0.07 * p * (1-p),
    "prophet":   lambda p: 0.02 * p,           # 2% on winnings
    "betopenly": lambda p: 0.02 * p,           # verify at signup
    "rebet":     lambda p: 0.03 * p,           # verify
    "polymarket":lambda p: 0.02 * p * (1-p),
}


def american_to_prob(o):
    return -o/(-o+100) if o < 0 else 100/(o+100)


def get_kalshi_games():
    """Pull all live Kalshi sports games grouped by event."""
    all_games = []
    for sport in ['mlb','nfl','nba','wnba','nhl','mls','ncaaf','ncaab','ufc']:
        series_map = {"mlb":"KXMLBGAME","nfl":"KXNFLGAME","nba":"KXNBAGAME","wnba":"KXWNBAGAME",
                      "nhl":"KXNHLGAME","mls":"KXMLSGAME","ncaaf":"KXNCAAFGAME",
                      "ncaab":"KXNCAABGAME","ufc":"KXUFCFIGHT"}
        stk = series_map[sport]
        try:
            r = requests.get(f"{KALSHI}/markets", params={"series_ticker":stk,"status":"open","limit":100}, headers=UA, timeout=15)
            ms = r.json().get('markets') or []
        except Exception:
            continue
        events = {}
        for m in ms:
            ev = m.get('event_ticker','')
            if ev not in events: events[ev] = []
            def fv(*ks):
                for k in ks:
                    v = m.get(k)
                    if v not in (None,"","0.0000"):
                        try:
                            x = float(v)
                            return x/100 if x>1.5 else x
                        except: pass
                return None
            events[ev].append({'ticker':m.get('ticker'),'team':m.get('yes_sub_title','')[:22],
                               'yes_bid':fv('yes_bid_dollars','yes_bid'),
                               'yes_ask':fv('yes_ask_dollars','yes_ask'),
                               'close':m.get('close_time','')[:16]})
        for ev, sides in events.items():
            valid = [s for s in sides if s.get('yes_bid') is not None and s.get('yes_ask') is not None]
            if len(valid) >= 2:
                spread = sum(s['yes_ask']-s['yes_bid'] for s in valid) / len(valid)
                all_games.append({'sport':sport.upper(),'event':ev,'sides':valid,
                                  'close':valid[0].get('close',''),'avg_spread':spread})
    all_games.sort(key=lambda g:-g['avg_spread'])
    return all_games


def compute_arb(price_a, price_b, capital, fee_a=0, fee_b=0):
    if price_a<=0 or price_b<=0 or price_a>=1 or price_b>=1: return None
    ratio = (1+price_b) / (1+price_a)
    n_a = capital / (price_a + price_b/ratio)
    n_b = n_a / ratio
    cost_a, cost_b = n_a*price_a, n_b*price_b
    f_a, f_b = n_a*fee_a, n_b*fee_b
    total = cost_a + cost_b + f_a + f_b
    p_yes = n_a - total
    p_no  = n_b - total
    guaranteed = min(p_yes, p_no)
    return {
        'n_a': n_a, 'n_b': n_b, 'cost_a': cost_a, 'cost_b': cost_b,
        'fee_a': f_a, 'fee_b': f_b, 'total': total,
        'payoff_a_wins': p_yes, 'payoff_b_wins': p_no,
        'guaranteed': guaranteed, 'roc_pct': guaranteed/total*100 if total>0 else 0,
        'sum': price_a + price_b, 'edge_pre_fees_pct': (1-price_a-price_b)*100,
    }


def games_table(games, page=0, per_page=15):
    t = Table(title="Live Kalshi Sports Games (sorted by widest spread = best arb candidates)",
              box=box.ROUNDED, show_header=True, header_style="bold cyan")
    t.add_column("#", style="dim", width=3)
    t.add_column("Sport", width=6)
    t.add_column("Matchup", width=42)
    t.add_column("Kalshi Prices (yes bid/ask)", width=40)
    t.add_column("Spread", width=7, style="yellow")
    t.add_column("Close", width=17, style="dim")

    start = page * per_page
    for i, g in enumerate(games[start:start+per_page], start=1):
        matchup = " vs ".join(s['team'][:18] for s in g['sides'][:2])
        prices = " | ".join(f"{s['team'][:10]}: {s['yes_bid']:.2f}/{s['yes_ask']:.2f}" for s in g['sides'][:2])
        spread_color = "green" if g['avg_spread'] > 0.05 else "yellow" if g['avg_spread'] > 0.03 else "dim"
        t.add_row(str(i), g['sport'], matchup, prices, f"[{spread_color}]{g['avg_spread']:.3f}[/]",
                  g['close'])
    return t


def show_arb_result(game_name, team, kalshi_bid, kalshi_ask, venue, venue_yes_prob, capital):
    """Compute both arb directions and show the actionable one."""
    kalshi_fee = FEES['kalshi']
    venue_fee = FEES.get(venue, lambda p: 0.02*p)

    # Direction 1: Buy YES Kalshi @ ask, Buy NO other venue @ (1 - venue_yes)
    d1 = compute_arb(kalshi_ask, 1-venue_yes_prob, capital,
                     fee_a=kalshi_fee(kalshi_ask), fee_b=venue_fee(1-venue_yes_prob))
    # Direction 2: Buy NO Kalshi @ (1 - bid), Buy YES other venue @ venue_yes
    d2 = compute_arb(1-kalshi_bid, venue_yes_prob, capital,
                     fee_a=kalshi_fee(1-kalshi_bid), fee_b=venue_fee(venue_yes_prob))

    best = None
    best_dir = None
    if d1 and d1['guaranteed'] > 0: best, best_dir = d1, 1
    if d2 and d2['guaranteed'] > 0 and (not best or d2['guaranteed'] > best['guaranteed']):
        best, best_dir = d2, 2

    if best:
        panel_color = "green"
        title = f"[bold green]✓ LOCKED ARB[/] ${best['guaranteed']:.2f} on ${best['total']:.2f} = {best['roc_pct']:+.2f}% ROC"
        if best_dir == 1:
            action = (f"[bold]EXECUTE (do both within 60 seconds):[/]\n\n"
                      f"  1) [cyan]KALSHI:[/] Buy [bold]{best['n_a']:.0f} YES[/] on {team}\n"
                      f"     price ~[green]{kalshi_ask:.3f}[/], cost ~${best['cost_a']:.2f}\n\n"
                      f"  2) [cyan]{venue.upper()}:[/] Bet on {team.split()[-1] if len(team.split())>1 else 'OTHER TEAM'} to WIN (the OPPOSITE side)\n"
                      f"     stake ~${best['cost_b']:.2f}\n\n"
                      f"[dim]Payoff if {team} wins: +${best['payoff_a_wins']:.2f}[/]\n"
                      f"[dim]Payoff if opposite wins: +${best['payoff_b_wins']:.2f}[/]")
        else:
            action = (f"[bold]EXECUTE (do both within 60 seconds):[/]\n\n"
                      f"  1) [cyan]KALSHI:[/] Buy [bold]{best['n_a']:.0f} NO[/] on {team} (= buy other team YES)\n"
                      f"     cost ~[green]{1-kalshi_bid:.3f}[/], total ~${best['cost_a']:.2f}\n\n"
                      f"  2) [cyan]{venue.upper()}:[/] Bet on {team} to WIN\n"
                      f"     stake ~${best['cost_b']:.2f}\n\n"
                      f"[dim]Payoff if opposite wins: +${best['payoff_a_wins']:.2f}[/]\n"
                      f"[dim]Payoff if {team} wins: +${best['payoff_b_wins']:.2f}[/]")
    else:
        panel_color = "red"
        title = "[bold red]✗ NO ARB[/]"
        worst1 = d1['guaranteed'] if d1 else 0
        worst2 = d2['guaranteed'] if d2 else 0
        action = (f"Direction 1 (YES Kalshi + NO {venue}): guaranteed [red]${worst1:.2f}[/]\n"
                  f"Direction 2 (NO Kalshi + YES {venue}): guaranteed [red]${worst2:.2f}[/]\n\n"
                  f"[dim]Fees + spread eat any edge. Skip this game.[/]")

    console.print(Panel(action, title=title, border_style=panel_color, box=box.DOUBLE))

    if best:
        log = Prompt.ask("\n[bold]Log this arb? (y/N)[/]", default="n")
        if log.lower() == 'y':
            log_arb(game_name, team, venue, best, best_dir)
            console.print("[green]✓ Logged to arb_log.csv[/]")


def log_arb(game, team, venue, r, direction):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['timestamp','game','team','venue','direction','n_kalshi','n_venue',
                        'cost_kalshi','cost_venue','total','projected_profit','roc_pct'])
        w.writerow([datetime.now().isoformat(timespec='seconds'), game, team, venue, direction,
                    f"{r['n_a']:.0f}", f"{r['n_b']:.0f}", f"{r['cost_a']:.2f}", f"{r['cost_b']:.2f}",
                    f"{r['total']:.2f}", f"{r['guaranteed']:.2f}", f"{r['roc_pct']:.2f}"])


def show_summary_stats():
    if not os.path.exists(LOG_FILE): return
    with open(LOG_FILE) as f:
        rows = list(csv.DictReader(f))
    if not rows: return
    total_projected = sum(float(r['projected_profit']) for r in rows)
    total_deployed = sum(float(r['total']) for r in rows)
    t = Table(box=box.ROUNDED, title="[bold]Your Arb Log[/]")
    t.add_column("Metric"); t.add_column("Value", style="green")
    t.add_row("Total arbs logged", str(len(rows)))
    t.add_row("Total capital deployed", f"${total_deployed:.2f}")
    t.add_row("Total projected profit", f"${total_projected:.2f}")
    t.add_row("Avg ROC per arb", f"{total_projected/total_deployed*100:.2f}%" if total_deployed else "-")
    console.print(t)


def main():
    console.print(Panel.fit("[bold cyan]Multi-Venue Sports Arb Dashboard[/]\n"
                             "[dim]Kalshi live feed + manual P2P price entry[/]",
                             border_style="cyan"))

    while True:
        console.print("\n[dim]Loading Kalshi games...[/]")
        games = get_kalshi_games()

        if not games:
            console.print("[red]No games loaded. Check network / Kalshi API status.[/]")
            return

        console.clear()
        console.print(Panel.fit("[bold cyan]Multi-Venue Sports Arb Dashboard[/]", border_style="cyan"))
        console.print(games_table(games))
        show_summary_stats()

        console.print("\n[bold]Options:[/]")
        console.print("  [cyan]<number>[/] — Check arb on that game")
        console.print("  [cyan]r[/] — Refresh Kalshi data")
        console.print("  [cyan]s[/] — Show arb log summary")
        console.print("  [cyan]q[/] — Quit")

        choice = Prompt.ask("\n[bold]Your choice[/]").strip().lower()

        if choice == 'q': break
        if choice == 'r': continue
        if choice == 's':
            show_summary_stats()
            Prompt.ask("\n[dim]Press Enter to continue[/]", default="")
            continue

        try:
            n = int(choice)
            if n < 1 or n > min(15, len(games)):
                console.print("[red]Invalid number[/]"); continue
        except ValueError:
            continue

        game = games[n-1]
        console.print(f"\n[bold]Selected:[/] {game['sport']} - {' vs '.join(s['team'] for s in game['sides'])}")
        console.print("Sides:")
        for i, s in enumerate(game['sides'], 1):
            console.print(f"  [{i}] [cyan]{s['team']}[/] Kalshi yes: [green]{s['yes_bid']:.3f}[/]/[red]{s['yes_ask']:.3f}[/]")

        side_choice = IntPrompt.ask("[bold]Which team do you want to check?[/]", default=1)
        if side_choice < 1 or side_choice > len(game['sides']): continue
        picked = game['sides'][side_choice-1]

        venue = Prompt.ask("\n[bold]Which other venue?[/] (prophet/betopenly/rebet/polymarket)",
                          choices=['prophet','betopenly','rebet','polymarket'], default='prophet')

        price_type = Prompt.ask("[bold]Enter price as[/]", choices=['american','decimal','probability'],
                               default='american')

        if price_type == 'american':
            am = IntPrompt.ask(f"[bold]{venue.title()} american odds for {picked['team']}[/] (e.g., -125 or +150)")
            venue_prob = american_to_prob(am)
            console.print(f"[dim]→ Implied probability: {venue_prob:.3f}[/]")
        elif price_type == 'decimal':
            dec = FloatPrompt.ask(f"[bold]{venue.title()} decimal odds for {picked['team']}[/]")
            venue_prob = 1/dec if dec > 0 else 0
            console.print(f"[dim]→ Implied probability: {venue_prob:.3f}[/]")
        else:
            venue_prob = FloatPrompt.ask(f"[bold]{venue.title()} YES probability for {picked['team']}[/] (0-1)")

        capital = FloatPrompt.ask("[bold]Capital to deploy on this arb[/]", default=200.0)

        show_arb_result(game['event'], picked['team'], picked['yes_bid'], picked['yes_ask'],
                        venue, venue_prob, capital)

        Prompt.ask("\n[dim]Press Enter to return to game list[/]", default="")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Exit.[/]")
