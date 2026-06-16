"""Efficiency and loss calculations — each accepts a DataFrame and returns a DataFrame."""

import re

import numpy as np
import pandas as pd

from src import config

_INV_KW_COL_RE = re.compile(r"^Inverter \d+ AC kW$")


def _inverter_kw_cols(df: pd.DataFrame) -> list:
    cols = [c for c in df.columns if _INV_KW_COL_RE.match(c)]
    return sorted(cols, key=lambda c: int(re.search(r"\d+", c).group()))


def calculate_efficiency(df: pd.DataFrame) -> pd.DataFrame:
    """Add INVERTER_TOTAL_KW and EFFICIENCY_PCT columns to the DataFrame."""
    df = df.copy()

    df[config.COL_TOTAL_INVERTER_KW] = df[_inverter_kw_cols(df)].sum(axis=1)

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


def calculate_daily_flags(enriched_df: pd.DataFrame, raw_df: pd.DataFrame) -> pd.DataFrame:
    """Return a per-day quality table flagging days with insufficient clean coverage.

    Daylight intervals = rows in raw_df where any inverter was producing (inv total > 0).
    Clean intervals    = rows in enriched_df (survived all filters) for that date.
    A day is 'good' if clean / daylight >= GOOD_DAY_MIN_CLEAN_PCT.
    """
    inv_cols = _inverter_kw_cols(raw_df)
    if not inv_cols:
        print("[calculator] WARNING: no inverter kW columns in raw_df — skipping daily flags")
        return pd.DataFrame()

    has_production = raw_df[inv_cols].sum(axis=1) > 0
    raw_dates      = pd.to_datetime(raw_df[config.COL_TIMESTAMP]).dt.date
    enr_dates      = pd.to_datetime(enriched_df[config.COL_TIMESTAMP]).dt.date

    daylight = (
        raw_dates[has_production]
        .value_counts()
        .sort_index()
        .rename("daylight_intervals")
        .rename_axis("date")
        .reset_index()
    )

    clean_counts = (
        enr_dates
        .value_counts()
        .sort_index()
        .rename("clean_intervals")
        .rename_axis("date")
        .reset_index()
    )

    daily_eff = (
        enriched_df
        .groupby(enr_dates)[config.COL_EFFICIENCY_PCT]
        .mean()
        .round(3)
        .rename("avg_efficiency_pct")
        .rename_axis("date")
        .reset_index()
    )

    daily = (
        daylight
        .merge(clean_counts, on="date", how="left")
        .merge(daily_eff,    on="date", how="left")
    )
    daily["clean_intervals"]  = daily["clean_intervals"].fillna(0).astype(int)
    daily["pct_clean"]        = (daily["clean_intervals"] / daily["daylight_intervals"]).round(4)
    daily["is_good_day"]      = daily["pct_clean"] >= config.GOOD_DAY_MIN_CLEAN_PCT

    n_good  = daily["is_good_day"].sum()
    n_total = len(daily)
    print(
        f"[calculator] daily quality   : {n_good}/{n_total} good days "
        f"({n_good / n_total * 100:.1f}%)\n"
    )

    return daily.sort_values("date").reset_index(drop=True)


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
