"""Cleaning filters — each accepts a DataFrame and returns a filtered DataFrame.

Filters are pure: they never mutate the input DataFrame.
All logic is derived from raw sensor columns; no pre-existing flag columns are used.
"""

import re

import pandas as pd

from src import config

_INV_KW_COL_RE = re.compile(r"^Inverter \d+ AC kW$")


def _inverter_kw_cols(df: pd.DataFrame) -> list:
    """Return inverter kW column names present in df, sorted by inverter number.

    Works for both standard DAS format (3 columns) and ACE format (N columns).
    Falls back to config.INVERTER_KW_COLS if no matching columns are found.
    """
    cols = [c for c in df.columns if _INV_KW_COL_RE.match(c)]
    if not cols:
        return config.INVERTER_KW_COLS
    return sorted(cols, key=lambda c: int(re.search(r"\d+", c).group()))


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
    all_inv_zero = (df[_inverter_kw_cols(df)] == 0).all(axis=1)
    meter_zero_or_neg = df[config.COL_METER_PRODUCTION_KW] <= 0
    # Keep the row unless all inverters and the meter are zero simultaneously.
    result = df[~(all_inv_zero & meter_zero_or_neg)].copy()
    print(f"  filter_offline        : dropped {before - len(result):>6,} rows "
          f"(both inverters + meter = 0)")
    return result


def _imbalance_flagged(df: pd.DataFrame, cols, threshold: float) -> pd.Series:
    """Return a boolean Series: True where (max-min)/mean exceeds threshold.

    Rows where mean is 0 produce NaN (division guard) and are treated as not-flagged —
    they will have been caught by filter_offline or filter_nighttime first.
    """
    vals = df[cols]
    mean = vals.mean(axis=1)
    ratio = (vals.max(axis=1) - vals.min(axis=1)) / mean.where(mean != 0)
    return (ratio > threshold).fillna(False)


def filter_phase_imbalance(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows where any signal group exceeds its imbalance ratio threshold.

    For standard DAS format: checks cross-inverter currents, voltages, and kW outputs.
    For ACE format: uses the precomputed per-inverter phase current flag written by the
    loader (config.COL_ACE_PHASE_IMBALANCE_FLAG) instead of the cross-inverter current check.
    """
    before = len(df)

    if config.COL_ACE_PHASE_IMBALANCE_FLAG in df.columns:
        current_flag = df[config.COL_ACE_PHASE_IMBALANCE_FLAG]
        current_note = (
            f"currents(per-inv phase)={current_flag.sum():,} "
            f"(threshold {config.ACE_PHASE_CURRENT_IMBALANCE_THRESHOLD:.0%})"
        )
    else:
        current_flag = _imbalance_flagged(
            df,
            [config.COL_CURRENT_A, config.COL_CURRENT_B, config.COL_CURRENT_C],
            config.CURRENT_IMBALANCE_THRESHOLD,
        )
        current_note = f"currents={current_flag.sum():,} (threshold {config.CURRENT_IMBALANCE_THRESHOLD:.0%})"

    voltage_flag = _imbalance_flagged(
        df,
        [config.COL_VOLTAGE_A, config.COL_VOLTAGE_B, config.COL_VOLTAGE_C],
        config.VOLTAGE_IMBALANCE_THRESHOLD,
    )

    inv_cols = _inverter_kw_cols(df)
    if len(inv_cols) >= 2:
        inverter_flag = _imbalance_flagged(df, inv_cols, config.INVERTER_IMBALANCE_THRESHOLD)
        inv_note = f"inverters={inverter_flag.sum():,} (threshold {config.INVERTER_IMBALANCE_THRESHOLD:.0%})"
    else:
        inverter_flag = pd.Series(False, index=df.index)
        inv_note = "inverters=skipped (only 1 inverter configured)"

    result = df[~(current_flag | voltage_flag | inverter_flag)].copy()
    dropped = before - len(result)
    print(f"  filter_phase_imbalance: dropped {dropped:>6,} rows")
    print(f"    by signal group     :  "
          f"{current_note}  "
          f"voltages={voltage_flag.sum():,} (threshold {config.VOLTAGE_IMBALANCE_THRESHOLD:.0%})  "
          f"{inv_note}  "
          f"(rows may overlap)")
    return result


def filter_gross_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows where inline-calculated efficiency is outside [MIN_EFFICIENCY_PCT, MAX_EFFICIENCY_PCT].

    Efficiency is computed fresh here as meter_kw / (inv1_kw + inv2_kw) * 100.
    Rows where inverter total is <= 0 produce NaN efficiency and are kept — they
    are already handled upstream by filter_offline.
    """
    before = len(df)
    inv_total = df[_inverter_kw_cols(df)].sum(axis=1)

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
