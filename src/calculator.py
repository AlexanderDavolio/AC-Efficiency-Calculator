"""Efficiency and loss calculations — each accepts a DataFrame and returns a DataFrame."""

import re

import numpy as np
import pandas as pd

from src import config

_INV_KW_COL_RE = re.compile(r"^Inverter \d+ AC kW$")


def _inverter_kw_cols(df: pd.DataFrame) -> list:
    """Return all 'Inverter N AC kW' columns sorted by inverter number."""
    cols = [c for c in df.columns if _INV_KW_COL_RE.match(c)]
    return sorted(cols, key=lambda c: int(re.search(r"\d+", c).group()))


# ── Daytime-only inverter telemetry detection ──────────────────────────────────
# Some sites' DAS records NOTHING for inverters at night — the channels are blank/NULL — rather
# than recording an explicit zero. The loaders sum the per-string kWh with sum(skipna=True), so
# an all-NULL nighttime row collapses to 0.0 in the derived "Inverter N AC kW" columns; by the
# time the cleaned/enriched frame exists the "absent" signal is gone (a daytime-only site looks
# identical to one that records zeros). To tell them apart we read the ORIGINAL per-string
# inverter columns the loader leaves in place ("Inverter 01", "Inverter (PS1) A", …) — NOT the
# derived "Inverter N AC kW" columns, whose NULLs are already 0.
#
# A daytime-only-telemetry site structurally caps its good-day fraction: nighttime/dawn
# intervals where the meter shows load but the inverters are blank can never be "clean", and the
# phase-current check sees only phantom nighttime currents. cleaners/reporter use the detection
# below to scope the good-day denominator and the phase filter to reporting (daytime) intervals.
# Detection is data-driven and site-agnostic — no site name appears anywhere.

_NIGHT_HOURS = range(0, 6)                  # 00:00–05:59 nighttime proxy
DAYTIME_ONLY_NIGHT_NULL_RATE = 0.95         # >95% nighttime NULL => daytime-only telemetry
_RAW_INV_NAME_RE = re.compile(r"inverter|\binv\b", re.IGNORECASE)


def _raw_inverter_telemetry_cols(df: pd.DataFrame) -> list:
    """Original per-string inverter columns that still carry NULLs.

    Excludes the derived 'Inverter N AC kW' columns (whose nighttime NULLs were collapsed to 0
    at load) and the plural 'Inverters' aggregate. These are the columns whose blank-vs-zero
    pattern reveals daytime-only telemetry.
    """
    cols = []
    for c in df.columns:
        name = str(c)
        if _INV_KW_COL_RE.match(name):          # derived kW channel — NULLs already 0
            continue
        if "inverters" in name.lower():         # plural aggregate, not a per-string channel
            continue
        if _RAW_INV_NAME_RE.search(name):
            cols.append(c)
    return cols


def telemetry_reporting_mask(df: pd.DataFrame) -> pd.Series:
    """Per-row boolean: at least one original inverter channel is non-null (the DAS is reporting
    that interval). For a daytime-only-telemetry site this marks the daytime window. Sites with
    no original per-string inverter columns are treated as always reporting (mask all True)."""
    cols = _raw_inverter_telemetry_cols(df)
    if not cols:
        return pd.Series(True, index=df.index)
    return df[cols].notna().any(axis=1)


def is_daytime_only_telemetry(df: pd.DataFrame) -> bool:
    """True when inverter telemetry is daytime-only: more than DAYTIME_ONLY_NIGHT_NULL_RATE of
    nighttime (hours 0–5) intervals have EVERY original inverter channel blank/NULL — the DAS
    records nothing at night instead of recording zero.

    Must run on a frame that still has the original inverter columns (the loaded raw_df), since
    the derived 'Inverter N AC kW' columns no longer carry the NULLs. Site-agnostic.
    """
    cols = _raw_inverter_telemetry_cols(df)
    if not cols or config.COL_TIMESTAMP not in df.columns:
        return False
    ts = pd.to_datetime(df[config.COL_TIMESTAMP], errors="coerce")
    night = ts.dt.hour.isin(_NIGHT_HOURS)
    if not night.any():
        return False
    night_rows = df.loc[night, cols]
    if night_rows.empty:
        return False
    null_rate = night_rows.isna().all(axis=1).mean()
    return bool(null_rate > DAYTIME_ONLY_NIGHT_NULL_RATE)


def calculate_efficiency(df: pd.DataFrame) -> pd.DataFrame:
    """Add INVERTER_TOTAL_KW and EFFICIENCY_PCT columns to the DataFrame.

    efficiency_pct = (meter_kw / inverter_total_kw) × 100

    Values < 100% represent AC losses between inverter terminals and the meter
    (wiring, transformer, etc.). Values > 100% indicate a data error — these rows
    are removed by filter_gross_outliers. Rows where inverter total is zero or
    negative produce NaN efficiency and are removed by filter_inverter_active.
    """
    df = df.copy()

    df[config.COL_TOTAL_INVERTER_KW] = df[_inverter_kw_cols(df)].sum(axis=1)

    # Guard against divide-by-zero; rows with non-positive inverter total become NaN.
    df[config.COL_EFFICIENCY_PCT] = (
        df[config.COL_METER_PRODUCTION_KW]
        / df[config.COL_TOTAL_INVERTER_KW].where(df[config.COL_TOTAL_INVERTER_KW] > 0)
        * 100
    )

    return df


def calculate_loss_delta(df: pd.DataFrame) -> pd.DataFrame:
    """Add LOSS_DELTA_KW, LOSS_PCT, and ENERGY_LOST_KWH columns.

    loss_delta_kw  = inverter_total_kw − meter_kw  (positive = expected loss)
    loss_pct       = loss_delta_kw / inverter_total_kw × 100
    energy_lost_kwh = loss_delta_kw × (INTERVAL_MINUTES / 60)

    Negative loss_delta means the meter reads higher than inverters, which warrants
    investigation (likely a sensor fault or meter/inverter mismatch).
    Requires calculate_efficiency to have run first so INVERTER_TOTAL_KW exists.
    """
    df = df.copy()

    df[config.COL_LOSS_DELTA_KW] = (
        df[config.COL_TOTAL_INVERTER_KW] - df[config.COL_METER_PRODUCTION_KW]
    )

    df[config.COL_LOSS_PCT] = (
        df[config.COL_LOSS_DELTA_KW]
        / df[config.COL_TOTAL_INVERTER_KW].where(df[config.COL_TOTAL_INVERTER_KW] > 0)
        * 100
    )

    df[config.COL_ENERGY_LOST_KWH] = df[config.COL_LOSS_DELTA_KW] * (config.INTERVAL_MINUTES / 60)

    return df


def add_time_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """Add MONTH (int) and TIME_BUCKET (str) columns derived from the timestamp."""
    df = df.copy()

    df[config.COL_MONTH] = df[config.COL_TIMESTAMP].dt.month

    hour = df[config.COL_TIMESTAMP].dt.hour
    df[config.COL_TIME_BUCKET] = np.select(
        condlist=[
            hour.between(6, 9),
            hour.between(10, 13),
            hour.between(14, 17),
        ],
        choicelist=["Morning", "Peak", "Afternoon"],
        default="Other",
    )

    return df


def run_all_calculations(df: pd.DataFrame) -> pd.DataFrame:
    """Run both calculations in dependency order and return the enriched DataFrame."""
    df = calculate_efficiency(df)
    df = calculate_loss_delta(df)
    df = add_time_buckets(df)

    return df
