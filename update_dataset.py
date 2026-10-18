"""Merge newly received ESP32 readings into persistent household_history.csv."""
import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
LIVE_FILE = ROOT / os.getenv("LIVE_LOG", "live_log.csv")
HISTORY_FILE = ROOT / os.getenv("HISTORY_FILE", "household_history.csv")
DEFAULT_HOUSE_ID = os.getenv("HOUSE_ID", "home_001")

REQUIRED = ["house_id", "timestamp", "total_w"]

def main():
    if not LIVE_FILE.exists():
        raise FileNotFoundError(
            f"{LIVE_FILE} not found. Start app.py and send ESP32 readings first."
        )

    live = pd.read_csv(LIVE_FILE)
    if live.empty:
        raise ValueError(f"{LIVE_FILE} has no readings yet.")
    if "house_id" not in live.columns:
        live["house_id"] = DEFAULT_HOUSE_ID
    live["house_id"] = live["house_id"].fillna(DEFAULT_HOUSE_ID).astype(str)

    for col in REQUIRED:
        if col not in live.columns:
            if col == "house_id":
                live[col] = DEFAULT_HOUSE_ID
            else:
                raise ValueError(f"Missing required column '{col}' in {LIVE_FILE}")

    live["timestamp"] = pd.to_datetime(live["timestamp"], errors="coerce", utc=True)
    live["total_w"] = pd.to_numeric(live["total_w"], errors="coerce")
    live = live.dropna(subset=["timestamp", "total_w"])
    if live.empty:
        raise ValueError("No valid timestamp/total_w rows found in live_log.csv.")

    if HISTORY_FILE.exists():
        history = pd.read_csv(HISTORY_FILE)
        if "house_id" not in history.columns:
            history["house_id"] = DEFAULT_HOUSE_ID
        if "timestamp" not in history.columns:
            history["timestamp"] = pd.NaT
        history["house_id"] = history["house_id"].fillna(DEFAULT_HOUSE_ID).astype(str)
        history["timestamp"] = pd.to_datetime(history["timestamp"], errors="coerce", utc=True)
        if "total_w" not in history.columns:
            history["total_w"] = pd.NA
        history["total_w"] = pd.to_numeric(history["total_w"], errors="coerce")
        all_cols = list(dict.fromkeys(list(history.columns) + list(live.columns)))
        history = history.reindex(columns=all_cols)
        live = live.reindex(columns=all_cols)
        combined = pd.concat([history, live], ignore_index=True)
    else:
        combined = live.copy()

    # Collapse duplicate exact timestamps for the same household; keep latest reading.
    combined = combined.dropna(subset=["timestamp", "total_w"])
    combined = combined.sort_values("timestamp")
    combined = combined.drop_duplicates(subset=["house_id", "timestamp"], keep="last")
    combined["is_weekend"] = (combined["timestamp"].dt.dayofweek >= 5).astype(int)
    combined["slot"] = (
        combined["timestamp"].dt.hour * 4 + combined["timestamp"].dt.minute // 15
    ).astype(int)
    combined.to_csv(HISTORY_FILE, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")

    print(f"Updated: {HISTORY_FILE}")
    print(f"Total retained readings: {len(combined)}")
    print(f"Households: {', '.join(sorted(combined['house_id'].unique()))}")
    print(f"Latest timestamp: {combined['timestamp'].max()}")
    print("Next: python train_peak_model.py")

if __name__ == "__main__":
    main()
