"""Efficiency and loss calculations — each accepts a DataFrame and returns a DataFrame."""

import numpy as np
import pandas as pd

from src import config


def calculate_efficiency(df: pd.DataFrame) -> pd.DataFrame:
    """Add INVERTER_TOTAL_KW and EFFICIENCY_PCT columns to the DataFrame."""
    df = df.copy()

    df[config.COL_TOTAL_INVERTER_KW] = df[config.INVERTER_KW_COLS].sum(axis=1)

    # Only divide where inverter total is positive; undefined rows become NaN.
    df[config.COL_EFFICIENCY_PCT] = (
        df[config.COL_METER_PRODUCTION_KW]
        / df[config.COL_TOTAL_INVERTER_KW].where(df[config.COL_TOTAL_INVERTER_KW] > 0)
        * 100
    )

    return df


def calculate_loss_delta(df: pd.DataFrame) -> pd.DataFrame:
    """Add LOSS_DELTA_KW column: inverter total minus meter production.

    Positive = inverters produced more than the meter recorded (expected losses).
    Negative = meter reads higher than inverters, which warrants investigation.
    Requires calculate_efficiency to have run first so INVERTER_TOTAL_KW exists.
    """
    df = df.copy()

    df[config.COL_LOSS_DELTA_KW] = (
        df[config.COL_TOTAL_INVERTER_KW] - df[config.COL_METER_PRODUCTION_KW]
    )

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

    avg_eff = df[config.COL_EFFICIENCY_PCT].mean()
    avg_loss = df[config.COL_LOSS_DELTA_KW].mean()
    print(
        f"[calculator] avg efficiency : {avg_eff:.2f}%"
        f"\n[calculator] avg loss delta : {avg_loss:.3f} kW\n"
    )

    return df
