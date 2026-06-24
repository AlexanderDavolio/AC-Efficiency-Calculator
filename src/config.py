from dataclasses import dataclass


@dataclass
class SiteConfig:
    """Per-site overrides for the ACE Built-In Query Report format.

    All fields are optional. A site may appear in SITE_CONFIGS purely to carry reporting
    metadata (e.g. excluded_months) while still using auto-detection for its columns:
    meter/inverter detection falls back to auto-detection whenever the corresponding
    pattern list is None, so leaving the patterns unset does not change ingestion.
    """
    meter_patterns:    list = None   # None = auto-detect the meter column(s)
    inverter_patterns: list = None   # None = auto-detect the inverter columns
    # Calendar months (list of "YYYY-MM") whose efficiency must NOT be reported — the data
    # exists and survives cleaning, but is known to be untrustworthy (e.g. a confirmed CT
    # fault corrupting the meter). These months are excluded from every reported efficiency
    # figure and surfaced in the Data Gaps section instead. Raw data and the cleaned CSV are
    # unaffected. None / empty = nothing excluded.
    excluded_months:   list = None


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
# 1 kW rather than 0 to absorb meter noise and pre-dawn ramp artefacts.
NIGHTTIME_KW_THRESHOLD = 1.0

# (max - min) / mean per signal group must not exceed these ratios.
# These are per-INVERTER measurements, not per-phase of a single 3-phase inverter —
# normal current spread across inverters can be 30–40% when string sizes differ.
CURRENT_IMBALANCE_THRESHOLD  = 0.50   # per-inverter AC current (loose — catches near-zero/offline inverter)
VOLTAGE_IMBALANCE_THRESHOLD  = 0.05   # per-inverter AC voltage (tight — voltage should always match grid)
INVERTER_IMBALANCE_THRESHOLD = 0.50   # per-inverter computed kW (loose — mirrors current threshold)

# Inline-calculated per-interval efficiency bounds. Rows outside [MIN, MAX] are
# discarded as sensor/CT garbage (dead meter, miswired CT), NOT real losses.
# This is a wide sensor-sanity band, not a performance band: individual intervals
# routinely read slightly over 100% due to meter/inverter interval-boundary timing
# jitter, and those are legitimate — they wash out under energy weighting. Keeping
# a tight band here (e.g. an exact 100% ceiling) would discard ~31% of valid
# high-production daytime intervals. Roll-up efficiency is energy-weighted, so
# only genuinely impossible readings need to be filtered at the interval level.
MIN_EFFICIENCY_PCT = 50.0
MAX_EFFICIENCY_PCT = 150.0

# Absolute physical bounds for a SINGLE inverter or meter channel's per-interval energy.
# cleaners.filter_value_spikes drops only the exact rows where a channel reads ABOVE
# MAX_INTERVAL_KWH or BELOW MIN_INTERVAL_KWH — a per-row check against these limits, with
# no context/neighbour logic and no site-specific behaviour. Anything inside the
# [MIN, MAX] range is always kept (a valid reading beside a spike, a recovering reading,
# a real production decline). These two values are the ONLY tuning knobs; a site at a
# different scale just adjusts them.
#
# 12,500 kWh in a 15-min interval is 50 MW of average power — far above any single Altus
# channel, so a value beyond it is a DAS glitch / sensor fault / register rollover, not
# production. The negative floor mirrors it: a real generation channel is never severely
# negative, so a large negative is a rollover/sentinel value. Raise the magnitudes for
# utility-scale sites; tighten the floor toward 0 if a site should never read negative.
MAX_INTERVAL_KWH = 12500.0
MIN_INTERVAL_KWH = -12500.0

# CT / inverter-communication dropout detection. When every inverter is online but one
# stops communicating with the others, its reported output collapses while the rest carry
# on — so the inverter total (and therefore the meter-vs-inverter comparison) for that
# interval is unreliable. cleaners.filter_inverter_comms drops a row if any inverter's
# share of total generation deviates from its baseline (median) share by more than this
# fraction. A real production change (clouds, curtailment) scales all inverters together
# and leaves their shares ~constant, so it is NOT flagged. 0.25 = flag a >25% relative
# swing in any one inverter's share; lower is stricter. Below MIN_GEN_KW_FOR_SHARE_CHECK
# the shares are too noisy to judge and the row is left alone.
MAX_INVERTER_SHARE_DEVIATION = 0.25

# CT / meter-communication dropout detection for sites with multiple generation-meter
# stations (e.g. PS1/PS2/PS3). Same idea as the inverter check but on the per-station
# meter columns: each station should hold a roughly stable share of total meter output, so
# if one station faults or drops out its share collapses and the aggregate meter reading
# is untrustworthy. cleaners.filter_meter_comms drops a row if any station's share of total
# meter output deviates from its baseline (median) share by more than this fraction.
# Fully automatic and per-interval — only the faulted intervals are dropped, not a whole
# window. Single-station sites are unaffected. 0.25 = flag a >25% relative swing.
MAX_METER_SHARE_DEVIATION = 0.25

# Minimum total output (kW) for a share-deviation check to apply — used by
# filter_inverter_comms (inverter generation), filter_meter_comms (meter output), AND
# filter_phase_current (per-phase current). At dawn/dusk the total is tiny and per-channel
# values swing wildly from ramp-timing differences alone, so these checks are skipped below
# this floor.
MIN_GEN_KW_FOR_SHARE_CHECK = 1.0

# Single-phase CT fault detection for meter stations that expose per-phase AC current
# (IacA/IacB/IacC). cleaners.filter_phase_current compares a station's three phase currents
# against each other at EVERY interval: a healthy three-phase service carries near-equal
# current on all three legs, so the per-row median of the three is a robust reference. A row
# is dropped if any phase deviates from that median by more than this fraction — the
# signature of one leg's CT dropping out or saturating while the others hold. This catches
# faults the energy-share check (MAX_METER_SHARE_DEVIATION) misses: a single phase is only
# ~1/3 of a station, so a phase dropout moves the station's TOTAL energy too little to trip
# the share check, yet shows up plainly leg-to-leg. 0.04 = flag a phase >4% off the median
# of the three. This is around the edge of normal three-phase imbalance, so it is aggressive.
# IMPORTANT — multi-station sites: French's Landfill (Brick) runs a wide leg-to-leg current
# imbalance, so in the FULL pipeline any value below ~0.10 leaves it with 0 good days (it
# needs ~0.20-0.30 to recover its ~105 good days). Single-meter sites (1247, 40 Twosome) are
# far less sensitive. So 0.04 trims hard everywhere and still wipes French's entirely. (0.30
# was the original default — only gross single-phase faults; 0.01/0.03 were tighter
# fault-isolation trials that also wiped French's.) Below MIN_GEN_KW_FOR_SHARE_CHECK the
# currents are too small and noisy to judge and the row is left alone. Stations missing any of
# the three phase columns (and sites with no per-phase current data) are skipped entirely.
MAX_PHASE_CURRENT_DEVIATION = 0.04

# Allowed deviation from equal inverter power share before flagging as imbalanced.
# E.g., with 3 inverters (equal share = 33.3%), a value of 5 flags anything outside 28–38%.
INVERTER_IMBALANCE_TOLERANCE_PP = 5

# Good-day methodology threshold. Reported efficiency (monthly AND overall) is computed over
# GOOD DAYS only. A producing day — one with >=1 interval whose meter reads above
# NIGHTTIME_KW_THRESHOLD — is "good" when at least this fraction of its production intervals
# survive every cleaning filter; otherwise it is a "bad" day and is excluded from the
# efficiency figures (its data still appears in the raw input and cleaned CSV). The reporter
# prints good-vs-bad day counts next to each month's efficiency and for the site overall. The
# filters themselves are unchanged; this only governs how the surviving intervals are rolled
# up. 0.70 = a day must have >=70% of its production intervals clean to count. Raising it
# yields a cleaner but smaller (more selection-biased) sample; lowering it admits dirtier days.
# See src/reporter.py (_day_quality / _keep_good_days).
GOOD_DAY_MIN_CLEAN_PCT = 0.70

# Minimum number of clean intervals a calendar month must have for its efficiency to be
# reported. A month with only a handful of surviving intervals yields a statistically
# meaningless figure (e.g. 131% from a few timing-jittered rows, or a "declining" trend
# that is really CT/comms dropouts), so months below this are not given a number — they
# are moved to the Data Gaps section as "insufficient clean intervals". 100 intervals is
# ~25 hours of 15-min data; raise for stricter sites, lower for sparse exports.
MIN_CLEAN_INTERVALS_PER_MONTH = 100

# Statistical anomaly detection for monthly efficiency. After the per-month figures are
# computed, the reporter takes the site's own median monthly efficiency and standard
# deviation and flags any month falling more than ANOMALY_STD_THRESHOLD standard
# deviations BELOW the median — a month statistically out of place against the site's own
# history, the signature of a meter/instrumentation fault rather than a real loss. Flagged
# months are dropped from the reported efficiency (monthly table and OVERALL) and moved to
# the Data Gaps section. Fully automatic and per-site — no dates, no site-specific config.
# 2.0 is a conventional outlier cut; lower flags more aggressively.
ANOMALY_STD_THRESHOLD = 2.0
# Minimum number of reported months required before anomaly detection runs at all — a
# median/std over too few months is not a meaningful baseline, so below this the check is
# skipped and every month is kept.
ANOMALY_MIN_MONTHS = 4

# ── Cumulative-register detection ────────────────────────────────────────────

# Some DAS exports deliver energy channels as cumulative lifetime registers (kWh that
# only counts up) instead of per-interval energy. The loaders difference such columns
# back to interval energy (see src/cumulative.py) before the kWh -> kW conversion.
#
# A column is treated as cumulative when, in timestamp order, it is non-decreasing
# across at least this fraction of consecutive steps. NOTE: this is deliberately far
# above a simple "majority" (>50%). Per-interval energy is itself non-decreasing
# ~75-85% of the time (flat zero runs all night, rising ramp every morning; only the
# afternoon declines), so a low threshold would misclassify healthy interval data as
# cumulative and corrupt it. A clean cumulative register is non-decreasing ~99.9% of
# the time (only resets/rollovers go backwards), so a high threshold separates the two
# without false positives.
CUMULATIVE_NONDECREASING_FRAC = 0.95
# A cumulative register must also strictly increase at least this often. This excludes
# dead all-zero or constant columns, which are trivially "non-decreasing".
CUMULATIVE_MIN_RISE_FRAC = 0.10
# Too few data points to judge monotonicity reliably -> assume per-interval (no convert).
CUMULATIVE_MIN_ROWS = 20

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
# Empty: all sites currently use auto-detection in excel_loader._auto_detect_inverter_cols.
ACE_INVERTER_COLUMN_PATTERNS = []

# Per-site overrides — keyed by sheet name (xlsx) or site_id assigned by the loader (CSV).
# Sites absent from this dict use auto-detection for meter and inverter columns.
SITE_CONFIGS: dict = {
    # French's Landfill is three power stations (PS1/PS2/PS3), each with its own
    # generation meter and inverters. The site production meter is the SUM of all three
    # generation meters. Auto-detection would otherwise pick only "Generation Meter
    # (PS1)" and divide it by all three stations' inverters (~39% — broken). The meter
    # pattern "Generation Meter" matches all three base meter columns (per-phase V/I and
    # power-factor sub-columns are filtered out by the loader); the inverter pattern
    # "Inverter (PS" matches all ten string columns "Inverter (PSn) X".
    "French's Landfill (Brick)": SiteConfig(
        meter_patterns=["Generation Meter"],
        inverter_patterns=["Inverter (PS"],
    ),
    # 40 Twosome Drive uses auto-detection for its columns (no patterns set). It is listed
    # here only to exclude Feb-Jun 2026 from efficiency reporting: a manager-confirmed CT
    # fault began at the site around Feb 2026, making the meter readings untrustworthy for
    # that window even though inverter communication looks normal. The data still loads and
    # is written to the cleaned CSV; it is just not reported as an efficiency figure.
    "40 Twosome Drive": SiteConfig(
        excluded_months=["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"],
    ),
}

# Curated, human-readable notes printed verbatim under a site's report (after its
# monthly table). Keyed by the site_id assigned by the loader. Use for known
# data-quality caveats that are not auto-derivable from the data alone — e.g. a specific
# inverter string's telemetry outage and what it means for the results.
SITE_NOTES: dict = {
    "French's Landfill (Brick)": [
        "Inverter string PS2 E had a telemetry gap from Oct 2024 through Jul 2025, "
        "reporting only ~12% of the time. Because the inverter-active filter requires "
        "every string to be reporting, this dropped the entire Oct 2024 - Jul 2025 "
        "window from the efficiency results. Meter data shows the site was generating "
        "normally throughout that window, so this is a monitoring/telemetry issue, not "
        "a production issue.",
    ],
    "40 Twosome Drive": [
        "Feb 2026 through Jun 2026 are excluded from efficiency reporting by decision: a "
        "confirmed CT fault began at the site around Feb 2026, making the production-meter "
        "readings untrustworthy for that window even though inverter communication looks "
        "normal. The data still loads but is not reported as an efficiency figure; see the "
        "Data Gaps section.",
    ],
}

# Hidden flag column written by the loader; True for rows where any inverter's phase
# currents (IacA/B/C) exceed the imbalance threshold.
COL_ACE_PHASE_IMBALANCE_FLAG = "_ace_phase_imbalance"

# Max absolute deviation from phase mean / mean must not exceed this for any inverter.
ACE_PHASE_CURRENT_IMBALANCE_THRESHOLD = 0.05
