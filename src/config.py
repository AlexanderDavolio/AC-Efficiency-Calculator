from dataclasses import dataclass


@dataclass
class SiteConfig:
    """Per-site ingestion parameters for ACE Built-In Query Report format."""
    meter_patterns: list    # case-insensitive substrings; first matching column wins
    expected_inverters: int  # warn if regex discovers a different count; 0 = skip check
    voltage_type: str       # "line_to_line" (VacAB/BC/CA) or "line_to_neutral" (VanA/B/C)


# Column name constants — edit here if the data schema changes; nowhere else.

# ── Raw input columns (present in the DAS Excel export) ─────────────────────

COL_TIMESTAMP = "Timestamp"

# Meter energy per interval — converted to average power (kW) in the loader.
COL_METER_KWH_RAW = "Production meter net energy Kilowatt hours"

# Per-inverter AC voltage and current — power is computed as V × A / 1000.
COL_VOLTAGE_A = "Inverter 1, AC voltage"
COL_CURRENT_A = "Inverter 1, AC current"
COL_VOLTAGE_B = "Inverter 2, AC voltage"
COL_CURRENT_B = "Inverter 2, AC current"
COL_VOLTAGE_C = "Inverter 3, AC voltage"
COL_CURRENT_C = "Inverter 3, AC current"

# ── Derived columns added by the loader ─────────────────────────────────────

# Meter average power: COL_METER_KWH_RAW × (60 / INTERVAL_MINUTES)
COL_METER_PRODUCTION_KW = "Meter kW"

# Per-inverter AC power: V × A / 1000 for each inverter
COL_INV1_AC_KW = "Inverter 1 AC kW"
COL_INV2_AC_KW = "Inverter 2 AC kW"
COL_INV3_AC_KW = "Inverter 3 AC kW"

# All inverter kW columns in order — add a new entry here for a fourth inverter.
INVERTER_KW_COLS = [
    COL_INV1_AC_KW,
    COL_INV2_AC_KW,
    COL_INV3_AC_KW,
]

# ── Data interval ────────────────────────────────────────────────────────────

# Measurement interval in minutes. Used to convert per-interval energy (kWh)
# to average power (kW): kW = kWh × (60 / INTERVAL_MINUTES).
INTERVAL_MINUTES = 15

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
# These are per-INVERTER measurements, not per-phase of a single 3-phase inverter —
# normal current spread across inverters can be 30–40% when string sizes differ.
CURRENT_IMBALANCE_THRESHOLD  = 0.50   # per-inverter AC current (loose — catches near-zero/offline inverter)
VOLTAGE_IMBALANCE_THRESHOLD  = 0.05   # per-inverter AC voltage (tight — voltage should always match grid)
INVERTER_IMBALANCE_THRESHOLD = 0.50   # per-inverter computed kW (loose — mirrors current threshold)

# Inline-calculated efficiency bounds. Rows outside [MIN, MAX] are gross outliers.
MIN_EFFICIENCY_PCT = 80.0
MAX_EFFICIENCY_PCT = 110.0

# Allowed deviation from equal inverter power share before flagging as imbalanced.
# E.g., with 3 inverters (equal share = 33.3%), a value of 5 flags anything outside 28–38%.
INVERTER_IMBALANCE_TOLERANCE_PP = 5

# ── ACE Built-In Query Report format ────────────────────────────────────────

# Meter column patterns searched in order when site_id is not in SITE_CONFIGS.
ACE_METER_COLUMN_PATTERNS = [
    "SEL-735",
    "METER - PRODUCTION",
    "Production Meter",
]

# Per-site overrides — keyed by sheet name (site_id).
# Sites absent from this dict get ACE_METER_COLUMN_PATTERNS + no inverter count check.
SITE_CONFIGS: dict = {
    "acedata4": SiteConfig(
        meter_patterns=["SEL-735"],
        expected_inverters=17,
        voltage_type="line_to_line",
    ),
}

# Hidden flag column written by the loader; True for rows where any inverter's phase
# currents (IacA/B/C) exceed the imbalance threshold.
COL_ACE_PHASE_IMBALANCE_FLAG = "_ace_phase_imbalance"

# Max absolute deviation from phase mean / mean must not exceed this for any inverter.
ACE_PHASE_CURRENT_IMBALANCE_THRESHOLD = 0.05
