#!/usr/bin/env python3
"""H1-H4: does spot/perp data predict outcomes beyond what the Kalshi ask prices?"""
import research as R

cands = R.build()
cfg = R.B.live_config()
sel = R.select(cands, cfg)

R.report("H1  vol-normalised distance to strike  z = sign*ln(S/K) / (sigma*sqrt(tau/60))",
         sel, lambda t: t["z"])

R.report("H2  adverse 3-min spot momentum before entry (higher = moving against us)",
         sel, lambda t: t["m3"])

R.report("H3  trailing 60-min realised vol (sigma, bps per minute)",
         sel, lambda t: t["sigma"] * 1e4)

alts = [t for t in sel if t["series"] != "KXBTC15M"]
R.report("H4  adverse BTC 3-min move at entry, alt series only",
         alts, lambda t: t["btc_m3"])

# robustness on H1: concurrent-minute z (what a live bot with real-time spot sees)
R.report("H1b  same but using the concurrent minute (upper bound, mild look-ahead)",
         sel, lambda t: t["z_conc"])

# H1 expressed as the model's disagreement with the ask
R.report("H1c  model edge = Phi(z) - ask/100",
         sel, lambda t: t["edge_model"])
