#!/usr/bin/env python3
"""Re-run exhaust optimizer with expanded pool (mega-harvest hits + original)."""
import subprocess, sys
from pathlib import Path
import pandas as pd

BASE = Path(__file__).parent

# Merge mega-harvest hits into wallet_hunt_report
hunt = pd.read_csv(BASE / "wallet_hunt_report.csv") if (BASE / "wallet_hunt_report.csv").exists() else pd.DataFrame()
mega = pd.read_csv(BASE / "mega_harvest_hits.csv") if (BASE / "mega_harvest_hits.csv").exists() else pd.DataFrame()

if len(mega):
    # Normalize mega columns to match hunt
    mega_norm = mega.rename(columns={
        "pnl_c": "pnl_corrected",
        "h1_edge": "hard_1_edge",
        "h3_sig": "hard_3_sig",
        "h4_samp": "hard_4_sample",
        "top_share": "top_month_share",
    })
    mega_norm["source"] = "mega_harvest"
    for col in hunt.columns:
        if col not in mega_norm.columns:
            mega_norm[col] = None
    combined = pd.concat([hunt, mega_norm[hunt.columns]], ignore_index=True)
    combined = combined.drop_duplicates(subset=["wallet"], keep="first")
    combined.to_csv(BASE / "wallet_hunt_report.csv", index=False)
    print(f"merged {len(mega)} mega-harvest hits into hunt report ({len(combined)} total)")

# Now re-run exhaust
print("re-running exhaust optimizer with expanded pool...")
r = subprocess.run(["python", str(BASE / "exhaust_optimizer.py")], cwd=str(BASE))
print(f"exit code {r.returncode}")
