#!/usr/bin/env python3
# Weather-edge test: does your model probability beat the market price, net of Kalshi fee?
# Auto-detects prediction / market-price / outcome columns so it runs on your schema.
import sys, math, pandas as pd, numpy as np

path = sys.argv[1] if len(sys.argv) > 1 else "kalshi_calibration.csv"
thr_args = [float(x) for x in sys.argv[2:]] or [0.0, 0.03, 0.05, 0.08]
df = pd.read_csv(path)
print("columns:", list(df.columns))
print(df.head(3).to_string(), "\n")

def pick(names, subs, need_numeric=True, rng01=False):
    for c in names:
        if any(s in c.lower() for s in subs):
            col = df[c]
            if need_numeric and not np.issubdtype(pd.to_numeric(col, errors="coerce").dtype, np.number):
                continue
            return c
    return None

pred_c  = pick(df.columns, ("pred","prob","model","forecast","ev_pred","p_model"))
price_c = pick(df.columns, ("price","yes_price","mkt","market","cost","entry","implied"))
out_c   = pick(df.columns, ("won","win","result","resolved","outcome","hit","y_true","label"))
print(f"detected -> pred='{pred_c}'  price='{price_c}'  outcome='{out_c}'")
if not pred_c or not out_c:
    sys.exit("need at least a prediction col and an outcome col — paste head -1 and I'll pin names")

def to01(s):
    s = pd.to_numeric(s, errors="coerce")
    if s.dropna().abs().max() > 1.5:   # looks like 0-100
        s = s / 100.0
    return s

pred = to01(df[pred_c])
out  = pd.to_numeric(df[out_c], errors="coerce").clip(0,1)
mask = pred.notna() & out.notna()
pred, out = pred[mask], out[mask]
n = len(pred)
brier_model = float(((pred - out)**2).mean())
print(f"\nn = {n} resolved markets")
print(f"model Brier score      : {brier_model:.4f}   (lower = better; 0.25 = coin flip)")

# reliability table
q = pd.qcut(pred, min(10, pred.nunique()), duplicates="drop")
rel = pd.DataFrame({"pred":pred,"out":out,"b":q}).groupby("b", observed=True).agg(
    n=("out","size"), mean_pred=("pred","mean"), emp_rate=("out","mean")).round(3)
print("\ncalibration (predicted vs realized):")
print(rel.to_string())

if price_c is not None:
    price = to01(df[price_c])[mask]
    m2 = price.notna()
    p_, o_, pr_ = pred[m2], out[m2], price[m2]
    brier_mkt = float(((pr_ - o_)**2).mean())
    print(f"\nmarket Brier score     : {brier_mkt:.4f}")
    verdict = "MODEL SHARPER THAN MARKET" if brier_model < brier_mkt else "market sharper — no forecasting edge"
    print(f"forecast edge check    : {verdict}  (model {brier_model:.4f} vs market {brier_mkt:.4f})")

    def kfee(p):  # Kalshi taker fee per 1 contract, rounded up to the cent
        return math.ceil(0.07 * p * (1-p) * 100) / 100

    print("\nnet-of-fee strategy (bet the side your model favors when |edge| > threshold):")
    print(f"{'thr':>5} {'trades':>7} {'net/tr':>8} {'net_total':>10} {'hit':>6}")
    for thr in thr_args:
        edge = p_ - pr_
        rows = []
        for e, o, pr in zip(edge, o_, pr_):
            if e > thr:      # buy YES
                rows.append((o - pr) - kfee(pr))
            elif -e > thr:   # buy NO
                rows.append((pr - o) - kfee(pr))
        if not rows:
            print(f"{thr:>5.2f} {0:>7}"); continue
        s = pd.Series(rows)
        net_tr = s.mean()
        hit = float((s > 0).mean())
        print(f"{thr:>5.2f} {len(s):>7} {net_tr:>8.4f} {s.sum():>10.2f} {hit:>6.1%}")
else:
    print("\n(no market-price column found — ran model calibration only; "
          "add price to test edge vs market)")
