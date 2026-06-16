"""Cleaning filters — each accepts a DataFrame and returns a filtered DataFrame.

Filters are pure: they never mutate the input DataFrame.
All logic derives from raw sensor columns present in the DataFrame.
"""

import re

import pandas as pd

from src import config

_INV_KW_COL_RE = re.compile(r"^Inverter \d+ AC kW$")


def _inverter_kw_cols(df: pd.DataFrame) -> list:
    cols = [c for c in df.columns if _INV_KW_COL_RE.match(c)]
    return sorted(cols, key=lambda c: int(re.search(r"\d+", c).group()))


def _raw_inverter_cols(df: pd.DataFrame, site_id: str = "") -> list:
    """Return raw inverter kWh column names sorted by inverter number using site config patterns."""
    site_cfg = config.SITE_CONFIGS.get(site_id)
    inv_patterns = (
        site_cfg.inverter_patterns
        if site_cfg and site_cfg.inverter_patterns is not None
        else config.ACE_INVERTER_COLUMN_PATTERNS
    )
    seen: dict = {}
    for c in df.columns:
        c_lc = c.lower()
        for pat in inv_patterns:
            if pat.lower() in c_lc:
                m = re.search(r"\d+", c)
                if m:
                    num = int(m.group())
                    if num not in seen:
                        seen[num] = c
                break
    return [c for _, c in sorted(seen.items(), key=lambda x: x[0])]


def filter_inverter_active(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop rows where any inverter reports zero or negative kWh (offline or nighttime)."""
    before = len(df)
    raw_cols = _raw_inverter_cols(df, site_id)
    mask = (df[raw_cols] > 0).all(axis=1)
    result = df[mask].copy()
    dropped = before - len(result)
    site_tag = f" [{site_id}]" if site_id else ""
    print(f"  filter_inverter_active       {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}")
    return result


def filter_gross_outliers(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop rows where inline-calculated efficiency is outside [MIN_EFFICIENCY_PCT, MAX_EFFICIENCY_PCT].

    Efficiency = meter_kw / total_inverter_kw * 100. Rows where inverter total is <= 0
    produce NaN efficiency and are kept — caught upstream by filter_inverter_active.
    """
    before = len(df)
    inv_total = df[_inverter_kw_cols(df)].sum(axis=1)
    eff = (df[config.COL_METER_PRODUCTION_KW] / inv_total.where(inv_total > 0)) * 100
    in_range = (eff >= config.MIN_EFFICIENCY_PCT) & (eff <= config.MAX_EFFICIENCY_PCT)
    result = df[in_range | eff.isna()].copy()
    dropped = before - len(result)
    site_tag = f" [{site_id}]" if site_id else ""
    print(f"  filter_gross_outliers        {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}")
    return result


def run_all_filters(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Apply all filters in order and return the cleaned DataFrame."""
    rows_in = len(df)
    site_tag = f" [{site_id}]" if site_id else ""
    print(f"\n[cleaners]{site_tag} starting: {rows_in:,} rows")

    df = filter_inverter_active(df, site_id)
    df = filter_gross_outliers(df, site_id)

    rows_out = len(df)
    print(
        f"[cleaners]{site_tag} finished: {rows_out:,} rows remaining "
        f"({rows_in - rows_out:,} total dropped, "
        f"{(rows_in - rows_out) / rows_in * 100:.1f}%)\n"
    )
    return df
