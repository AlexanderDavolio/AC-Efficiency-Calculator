# Column name constants — edit here if the CSV schema changes; nowhere else.

# ── Raw CSV column names ────────────────────────────────────────────────────

COL_TIMESTAMP = "Site Time"

COL_METER_PRODUCTION_KW = "METER - PRODUCTION, Active Power Kilowatts"

COL_INV1_AC_KW = "INVERTER 1, AC power Kilowatts"
COL_INV2_AC_KW = "INVERTER 2, AC power Kilowatts"

# Single source of truth for all inverter kW columns. Add a new entry here
# when a site has a third (or more) inverter — nothing else in the pipeline
# needs to change. COL_INV1_AC_KW / COL_INV2_AC_KW remain as named constants
# because the Excel workbook loader's required-column check references them directly.
INVERTER_KW_COLS = [
    COL_INV1_AC_KW,
    COL_INV2_AC_KW,
]

COL_CURRENT_A = "METER - PRODUCTION, AC Current A Amps"
COL_CURRENT_B = "METER - PRODUCTION, AC Current B Amps"
COL_CURRENT_C = "METER - PRODUCTION, AC Current C Amps"

COL_VOLTAGE_A = "METER - PRODUCTION, Voltage AN Volts"
COL_VOLTAGE_B = "METER - PRODUCTION, Voltage BN Volts"
COL_VOLTAGE_C = "METER - PRODUCTION, Voltage CN Volts"

# ── Derived / output column names ───────────────────────────────────────────

COL_TOTAL_INVERTER_KW = "total_inverter_kw"
COL_EFFICIENCY_PCT = "efficiency_pct"
COL_LOSS_DELTA_KW = "loss_delta_kw"
COL_MONTH = "MONTH"
COL_TIME_BUCKET = "TIME_BUCKET"

# ── Cleaning thresholds ─────────────────────────────────────────────────────

# Rows where meter production is strictly below this value are nighttime / offline.
NIGHTTIME_KW_THRESHOLD = 1.0

# (max - min) / mean per signal group must not exceed these ratios.
# Each group has its own threshold because normal operating spread differs by signal type.
CURRENT_IMBALANCE_THRESHOLD  = 0.03
VOLTAGE_IMBALANCE_THRESHOLD  = 0.05
INVERTER_IMBALANCE_THRESHOLD = 0.05

# Inline-calculated efficiency bounds. Rows outside [MIN, MAX] are gross outliers.
MIN_EFFICIENCY_PCT = 85.0
MAX_EFFICIENCY_PCT = 105.0

# Allowed deviation from equal inverter power share before flagging as imbalanced.
# E.g., with 2 inverters (equal share = 50%), a value of 5 flags anything outside 45–55%.
INVERTER_IMBALANCE_TOLERANCE_PP = 5
