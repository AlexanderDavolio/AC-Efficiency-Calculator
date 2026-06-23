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
