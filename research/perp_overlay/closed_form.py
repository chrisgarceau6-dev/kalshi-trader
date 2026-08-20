#!/usr/bin/env python3
"""Closed-form sanity check on the perp overlay, before the empirical run.

Binary payoff X = +w if S_T on the right side of K, else -bet.
Perp overlay pays u = -sign*r per $1 notional (gains when the market moves against us).
Under a driftless random walk E[u]=0, so the overlay's only EV effect is its cost.
"""
import math

bet   = 50.0
p     = 0.915          # entry price / implied win prob
fee   = 0.07
ct    = bet / p
w     = ct * (1 - p) * (1 - fee)      # net win
tau_s = 375.0                          # typical seconds left at entry
phi   = lambda z: math.exp(-z*z/2)/math.sqrt(2*math.pi)
Phi   = lambda z: 0.5*(1+math.erf(z/math.sqrt(2)))
zinv  = 1.372                          # Phi^-1(0.915)

print(f"win ${w:.2f}  loss ${bet:.2f}  break-even WR {bet/(bet+w)*100:.2f}%\n")
print(f"{'ann vol':>8} {'s(15m)':>8} {'dist to K':>10} {'Q*var-min':>11} "
      f"{'Q*cover':>10} {'sd cut':>7} {'cost@5bp':>9} {'edge/tr':>8}")
for vol in (0.30, 0.50, 0.80):
    s = vol * math.sqrt(tau_s / 31_536_000)     # sd of return over the hold
    d = zinv * s                                 # distance to strike implied by 91.5c
    # E[u 1{loss}] for a normal: loss branch is r < -d
    A = s * phi(zinv)                            # = E[|r| 1{r<-d}] magnitude
    cov = (w + bet) * A                          # Cov(X, u) with E[u]=0
    qvar = cov / (s * s)
    qcover = bet / (A / (1 - Phi(zinv)))         # notional s.t. mean loss is offset
    varX = p * (1 - p) * (w + bet) ** 2
    sd_cut = 1 - math.sqrt(max(varX - cov * cov / (s * s), 0)) / math.sqrt(varX)
    cost = qvar * 5 / 10000
    print(f"{vol*100:>7.0f}% {s*1e4:>7.1f}bp {d*1e4:>9.1f}bp {qvar:>11,.0f} "
          f"{qcover:>10,.0f} {sd_cut*100:>6.1f}% {cost:>9.2f} {0.59:>8.2f}")

print("\nread: 'cost@5bp' is what the var-min hedge costs per trade in perp fees")
print("      against an edge of +$0.59/trade. Any cost above that is a net loss.")
