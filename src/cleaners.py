"""Cleaning filters — each accepts a DataFrame and returns a filtered DataFrame.

Filters are pure: they never mutate the input DataFrame.
All logic is derived from raw sensor columns; no pre-existing flag columns are used.
"""

import pandas as pd

from src import config


def filter_nighttime(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows where meter production is below the minimum generation threshold."""
    before = len(df)
    mask = df[config.COL_METER_PRODUCTION_KW] >= config.NIGHTTIME_KW_THRESHOLD
    result = df[mask].copy()
    print(f"  filter_nighttime      : dropped {before - len(result):>6,} rows "
          f"(meter_kw < {config.NIGHTTIME_KW_THRESHOLD})")
    return result


def filter_offline(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows where both inverters and the meter are all at zero simultaneously."""
    before = len(df)
    both_inv_zero = (
        (df[config.COL_INV1_AC_KW] == 0) &
        (df[config.COL_INV2_AC_KW] == 0)
    )
    meter_zero_or_neg = df[config.COL_METER_PRODUCTION_KW] <= 0
    # Keep the row unless all three conditions fire at once.
    result = df[~(both_inv_zero & meter_zero_or_neg)].copy()
    print(f"  filter_offline        : dropped {before - len(result):>6,} rows "
          f"(both inverters + meter = 0)")
    return result


def filter_phase_imbalance(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows where the spread across the three AC phase currents exceeds the threshold.

    Ratio = (max - min) / mean across columns A, B, C for each row.
    Rows where mean is 0 (all phases zero) produce NaN and are kept — they were
    already caught by filter_offline.
    """
    before = len(df)
    phases = df[[config.COL_CURRENT_A, config.COL_CURRENT_B, config.COL_CURRENT_C]]
    phase_max = phases.max(axis=1)
    phase_min = phases.min(axis=1)
    phase_mean = phases.mean(axis=1)

    # Replace zero mean with NaN so division doesn't produce inf.
    ratio = (phase_max - phase_min) / phase_mean.where(phase_mean != 0)

    # NaN ratio (all-zero phases) passes through; only flag rows with a real excess.
    flagged = ratio > config.PHASE_IMBALANCE_RATIO_THRESHOLD
    result = df[~flagged.fillna(False)].copy()
    print(f"  filter_phase_imbalance: dropped {before - len(result):>6,} rows "
          f"(phase ratio > {config.PHASE_IMBALANCE_RATIO_THRESHOLD})")
    return result


def filter_gross_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows where inline-calculated efficiency is outside [MIN_EFFICIENCY_PCT, MAX_EFFICIENCY_PCT].

    Efficiency is computed fresh here as meter_kw / (inv1_kw + inv2_kw) * 100.
    Rows where inverter total is <= 0 produce NaN efficiency and are kept — they
    are already handled upstream by filter_offline.
    """
    before = len(df)
    inv_total = df[config.COL_INV1_AC_KW] + df[config.COL_INV2_AC_KW]

    # Only divide where inverter total is positive; everything else becomes NaN.
    eff = (df[config.COL_METER_PRODUCTION_KW] / inv_total.where(inv_total > 0)) * 100

    in_range = (eff >= config.MIN_EFFICIENCY_PCT) & (eff <= config.MAX_EFFICIENCY_PCT)
    result = df[in_range | eff.isna()].copy()
    print(f"  filter_gross_outliers : dropped {before - len(result):>6,} rows "
          f"(efficiency outside [{config.MIN_EFFICIENCY_PCT}%, {config.MAX_EFFICIENCY_PCT}%])")
    return result


def run_all_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all four filters in order and return the cleaned DataFrame."""
    rows_in = len(df)
    print(f"\n[cleaners] starting  : {rows_in:,} rows")

    df = filter_nighttime(df)
    df = filter_offline(df)
    df = filter_phase_imbalance(df)
    df = filter_gross_outliers(df)

    rows_out = len(df)
    print(f"[cleaners] finished  : {rows_out:,} rows remaining "
          f"({rows_in - rows_out:,} total dropped, "
          f"{(rows_in - rows_out) / rows_in * 100:.1f}%)\n")
    return df
