import csv, random, math
from datetime import datetime, timedelta

random.seed(42)
start = datetime(2026, 8, 15, 0, 0)
n = 60 * 96          # 60 days x 96 slots of 15 min = 5760 rows

def period(h):
    if 6 <= h < 10 or 18 <= h < 22: return "peak", 1.20
    if 10 <= h < 16: return "solar", 0.85
    return "normal", 1.00

rows = []
for k in range(n):
    t = start + timedelta(minutes=15 * k)
    h = t.hour + t.minute / 60
    weekend = t.weekday() >= 5
    per, mult = period(t.hour)
    volt = round(230 + random.uniform(-4, 4) + (-3 if 18 <= t.hour < 22 else 0), 1)

    base1 = 40 + 25 * math.exp(-((h - 19.5) ** 2) / 4) + 15 * math.exp(-((h - 7) ** 2) / 2)
    if 0 <= t.hour < 5: base1 *= 0.5
    p1 = max(0, base1 + random.gauss(0, 6)) * (1.15 if weekend else 1.0)

    p2 = 0.0
    if 6 <= h < 8.5 and random.random() < (0.55 if not weekend else 0.35): p2 = random.uniform(1300, 1600)
    elif 18 <= h < 21.5 and random.random() < 0.40: p2 = random.uniform(1200, 1500)
    elif weekend and 10 <= h < 13 and random.random() < 0.30: p2 = random.uniform(500, 900)
    elif random.random() < 0.03: p2 = random.uniform(150, 400)

    p1, p2 = round(p1, 1), round(p2, 1)
    i1, i2 = round(p1 / volt, 2), round(p2 / volt, 2)
    total = round(p1 + p2, 1)
    rows.append([t.strftime("%Y-%m-%d %H:%M:%S"), t.hour, t.strftime("%a"), int(weekend),
                 per, mult, volt, i1, i2, p1, p2, total, round(total * 0.25, 2)])

with open("household_load_synthetic.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["timestamp","hour","weekday","is_weekend","period","tod_multiplier",
                "voltage_v","load1_current_a","load2_current_a","load1_w","load2_w","total_w","energy_wh_15min"])
    w.writerows(rows)
print(len(rows), "rows written")
