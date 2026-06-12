# Column name constants — edit here if the CSV schema changes; nowhere else.

# ── Raw CSV column names ────────────────────────────────────────────────────

COL_TIMESTAMP = "Site Time"

COL_METER_PRODUCTION_KW = "METER - PRODUCTION, Active Power Kilowatts"

COL_INV1_AC_KW = "INVERTER 1, AC power Kilowatts"
COL_INV2_AC_KW = "INVERTER 2, AC power Kilowatts"

COL_CURRENT_A = "METER - PRODUCTION, AC Current A Amps"
COL_CURRENT_B = "METER - PRODUCTION, AC Current B Amps"
COL_CURRENT_C = "METER - PRODUCTION, AC Current C Amps"

# ── Derived / output column names ───────────────────────────────────────────

COL_TOTAL_INVERTER_KW = "total_inverter_kw"
COL_EFFICIENCY_PCT = "efficiency_pct"
COL_LOSS_DELTA_KW = "loss_delta_kw"
COL_MONTH = "MONTH"
COL_TIME_BUCKET = "TIME_BUCKET"

# ── Cleaning thresholds ─────────────────────────────────────────────────────

# Rows where meter production is strictly below this value are nighttime / offline.
NIGHTTIME_KW_THRESHOLD = 1.0

# (max - min) / mean across the three phase currents must not exceed this ratio.
# 0.05 = 5% spread; beyond that the row is treated as a sensor fault or grid anomaly.
PHASE_IMBALANCE_RATIO_THRESHOLD = 0.05

# Inline-calculated efficiency bounds. Rows outside [MIN, MAX] are gross outliers.
MIN_EFFICIENCY_PCT = 85.0
MAX_EFFICIENCY_PCT = 105.0
