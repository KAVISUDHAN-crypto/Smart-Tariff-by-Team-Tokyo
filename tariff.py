import time

# ---- EDIT THESE from your real TNEB bill / TNERC order ----
# (upper limit of bi-monthly units, ₹ per unit). Placeholder values.
SLABS = [(100, 0.0), (400, 4.70), (500, 6.30), (600, 8.40),
         (800, 9.45), (1000, 10.50), (float("inf"), 11.55)]
FIXED_CHARGE = 0.0            # domestic: none in the sources I found
ASSUMED_BIMONTHLY_UNITS = 350 # household's typical usage, used for the optimizer

# ---- ToD layer (assumption, clearly label it in the demo) ----
# (start_hour, end_hour, multiplier, name)
TOD = [(6, 10, 1.20, "peak"), (18, 22, 1.20, "peak"),
       (10, 16, 0.85, "solar"), ]          # everything else = 1.0 "normal"

def bill_bimonthly(units):
    """Real telescopic TNEB bill for a 2-month cycle."""
    total, prev = 0.0, 0
    for limit, rate in SLABS:
        if units > prev:
            total += (min(units, limit) - prev) * rate
        prev = limit
    return round(total + FIXED_CHARGE, 2)

def slab_rate(units):
    """Marginal ₹/unit at a given cumulative bi-monthly usage."""
    prev = 0
    for limit, rate in SLABS:
        if units <= limit:
            return rate
    return SLABS[-1][1]

def tod_multiplier(hour):
    for s, e, m, _ in TOD:
        if s <= hour < e:
            return m
    return 1.0

def rate_at(ts=None, units=ASSUMED_BIMONTHLY_UNITS):
    """₹/kWh at a moment = slab rate x ToD multiplier."""
    hour = time.localtime(ts or time.time()).tm_hour
    return slab_rate(units) * tod_multiplier(hour)
