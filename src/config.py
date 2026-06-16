from dataclasses import dataclass


@dataclass
class SiteConfig:
    """Per-site ingestion parameters for ACE Built-In Query Report format."""
    meter_patterns:    list = None   # None = fall back to ACE_METER_COLUMN_PATTERNS
    inverter_patterns: list = None   # None = fall back to ACE_INVERTER_COLUMN_PATTERNS


# Column name constants — edit here if the data schema changes; nowhere else.

# ── Raw input columns (present in the DAS Excel export) ─────────────────────

COL_TIMESTAMP = "Timestamp"

# Meter energy per interval — converted to average power (kW) in the loader.
COL_METER_KWH_RAW = "Production meter net energy Kilowatt hours"

# ── Derived columns added by the loader ─────────────────────────────────────

# Meter average power: COL_METER_KWH_RAW × (60 / INTERVAL_MINUTES)
COL_METER_PRODUCTION_KW = "Meter kW"

# ── Data interval ────────────────────────────────────────────────────────────

# Measurement interval in minutes. Used to convert per-interval energy (kWh)
# to average power (kW): kW = kWh × (60 / INTERVAL_MINUTES).
INTERVAL_MINUTES = 15

# ── Derived / output column names ───────────────────────────────────────────

COL_TOTAL_INVERTER_KW = "total_inverter_kw"
COL_EFFICIENCY_PCT = "efficiency_pct"
COL_LOSS_DELTA_KW = "loss_delta_kw"
COL_LOSS_PCT = "loss_pct"
COL_ENERGY_LOST_KWH = "energy_lost_kwh"
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

# Minimum fraction of expected daylight intervals that must be clean for a day to be "good".
# Days below this threshold are excluded from monthly and overall efficiency averages.
GOOD_DAY_MIN_CLEAN_PCT = 0.70

# ── ACE Built-In Query Report format ────────────────────────────────────────

# Meter column patterns searched in order when site_id is not in SITE_CONFIGS.
ACE_METER_COLUMN_PATTERNS = [
    "Wattnode Meter",
    "SEL-735",
    "METER - PRODUCTION",
    "Production Meter",
]

# Meter phase voltage column patterns (VacA, VacB, VacC).
ACE_METER_VOLTAGE_PATTERNS = [
    "VacA",
    "VacB",
    "VacC",
]

# Meter phase current column patterns (IacA, IacB, IacC).
ACE_METER_CURRENT_PATTERNS = [
    "IacA",
    "IacB",
    "IacC",
]

# Inverter kWh column patterns — case-insensitive substring match.
# Any column whose header contains one of these strings is treated as a per-inverter
# energy column. The first \d+ in the column name becomes the inverter number.
ACE_INVERTER_COLUMN_PATTERNS = [
    "SMA Inverter",
]

# Per-site overrides — keyed by sheet name (xlsx) or site_id assigned by the loader (CSV).
# Sites absent from this dict get the ACE_METER/INVERTER_COLUMN_PATTERNS defaults.
SITE_CONFIGS: dict = {
    "acedata4": SiteConfig(
        meter_patterns=["SEL-735"],
        inverter_patterns=["SMA Inverter"],
    ),
    # RGM-based site — inverter energy columns are named "RGM 01", "RGM 02", etc.
    # "Inverters" is used as the meter column (the aggregate kWh reading in this export).
    "Adams Farm": SiteConfig(
        meter_patterns=["Inverters"],
        inverter_patterns=["RGM"],
    ),
    "2 Commerce Drive": SiteConfig(
        inverter_patterns=["INVERTER"],
    ),
    "2 Executive Drive": SiteConfig(
        inverter_patterns=["INVERTER"],
    ),
}

# Hidden flag column written by the loader; True for rows where any inverter's phase
# currents (IacA/B/C) exceed the imbalance threshold.
COL_ACE_PHASE_IMBALANCE_FLAG = "_ace_phase_imbalance"

# Max absolute deviation from phase mean / mean must not exceed this for any inverter.
ACE_PHASE_CURRENT_IMBALANCE_THRESHOLD = 0.05
