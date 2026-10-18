import os
import sys
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score

SYN = sys.argv[1] if len(sys.argv) > 1 else "household_load_synthetic.csv"
LIVE = "live_log.csv"                                   # written by app.py from the ESP32
MULT = {"peak": 1.20, "normal": 1.00, "offpeak": 0.85}  # edit if you like
LIVE_WEIGHT = 10        # how much more a real 15-min reading counts than a synthetic one
MIN_LIVE_SLOTS = 16     # need at least 4 hours of real data before it is used
FEATS = ["slot", "is_weekend"]

def add_slot(d):
    d["slot"] = d["timestamp"].dt.hour * 4 + d["timestamp"].dt.minute // 15
    return d

def load_live():
    """Real sensor data -> one average total_w per 15-minute slot."""
    if not os.path.exists(LIVE):
        return None
    
    # FIX: Added on_bad_lines='skip' to ignore malformed strings or ESP32 boot text
    d = pd.read_csv(LIVE, parse_dates=["timestamp"], on_bad_lines="skip")
    
    if d.empty:
        return None
    d["timestamp"] = d["timestamp"].dt.floor("15min")
    g = d.groupby("timestamp", as_index=False)["total_w"].mean()
    g["is_weekend"] = (g["timestamp"].dt.weekday >= 5).astype(int)
    return add_slot(g)

# ---------- 1. choose the training data ----------
syn = add_slot(pd.read_csv(SYN, parse_dates=["timestamp"]))[["timestamp", "total_w", "is_weekend", "slot"]]
live = load_live()
live_slots = 0 if live is None else len(live)
live_days = 0 if live is None else live["timestamp"].dt.date.nunique()

if live is not None and live_days >= 7:
    mode, df, weights = "LIVE sensor data only", live, None
elif live is not None and live_slots >= MIN_LIVE_SLOTS:
    df = pd.concat([syn, live], ignore_index=True)
    weights = np.r_[np.ones(len(syn)), np.full(live_slots, float(LIVE_WEIGHT))]
    mode = f"BLEND: synthetic + {live_slots} live slots (live weighted x{LIVE_WEIGHT})"
else:
    mode, df, weights = "synthetic only (not enough live data yet)", syn, None

print(f"Mode: {mode}")
print(f"Live data: {live_slots} slots over {live_days} day(s)")

# ---------- 2. train (and test when the data allows a fair test) ----------
model = GradientBoostingRegressor(n_estimators=200, max_depth=3,
                                  learning_rate=0.05, random_state=0)
if weights is None:
    df = df.sort_values("timestamp").reset_index(drop=True)
    cut = int(len(df) * 0.8)
    train, test = df.iloc[:cut], df.iloc[cut:]
    model.fit(train[FEATS], train["total_w"])
    pred = model.predict(test[FEATS])
    avg = train.groupby(["slot", "is_weekend"])["total_w"].mean()
    base = test.apply(lambda r: avg.get((r["slot"], r["is_weekend"]), train["total_w"].mean()), axis=1)
    print(f"Rows: {len(df)}  (train {len(train)}, test {len(test)})")
    print(f"Model    MAE {mean_absolute_error(test['total_w'], pred):7.1f} W   R2 {r2_score(test['total_w'], pred):.4f}")
    print(f"Baseline MAE {mean_absolute_error(test['total_w'], base):7.1f} W   R2 {r2_score(test['total_w'], base):.4f}")
else:
    print("Accuracy test skipped: too little live data for a fair test.")

model.fit(df[FEATS], df["total_w"], sample_weight=weights)     # final fit on everything
joblib.dump(model, "load_model.joblib")

# ---------- 3. predict a typical day (weekday 5/7, weekend 2/7) ----------
slots = np.arange(96)
wd = model.predict(pd.DataFrame({"slot": slots, "is_weekend": 0}))
we = model.predict(pd.DataFrame({"slot": slots, "is_weekend": 1}))
hourly = ((5 * wd + 2 * we) / 7).reshape(24, 4).mean(axis=1)    # 24 values, W

# ---------- 4. label hours by predicted load ----------
PEAK_Q, OFFPEAK_Q = 0.75, 0.40
hi, lo = np.quantile(hourly, PEAK_Q), np.quantile(hourly, OFFPEAK_Q)
period = ["peak" if v >= hi else "offpeak" if v <= lo else "normal" for v in hourly]

pd.DataFrame({
    "hour": range(24),
    "start": [f"{h:02d}:00" for h in range(24)],
    "end": [f"{h + 1:02d}:00" for h in range(24)],
    "period": period,
    "multiplier": [MULT[p] for p in period],
    "predicted_w": hourly.round(1),
}).to_csv("peak_hours_ml.csv", index=False)

# ---------- 5. print the windows ----------
def windows(label):
    hrs = [h for h, p in enumerate(period) if p == label]
    runs, run = [], []
    for h in hrs:
        if run and h != run[-1] + 1:
            runs.append(run); run = []
        run.append(h)
    if run: runs.append(run)
    return ", ".join(f"{r[0]:02d}:00-{r[-1] + 1:02d}:00" for r in runs) or "none"

print("\nML-detected periods")
for label in ("peak", "normal", "offpeak"):
    print(f"  {label:8s} x{MULT[label]:.2f}: {windows(label)}")
print("\nSaved: peak_hours_ml.csv, load_model.joblib")
