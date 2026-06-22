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
    """Return raw inverter kWh column names sorted by inverter number.

    For unknown sites (or when SITE_CONFIGS is empty), returns the derived
    'Inverter N AC kW' columns that the loader already emitted — no raw re-detection needed.
    """
    site_cfg = config.SITE_CONFIGS.get(site_id)
    if site_cfg is None or site_cfg.inverter_patterns is None:
        return _inverter_kw_cols(df)

    seen: dict = {}
    for c in df.columns:
        c_lc = c.lower()
        for pat in site_cfg.inverter_patterns:
            if pat.lower() in c_lc:
                m = re.search(r"\d+", c)
                if m:
                    num = int(m.group())
                    seen.setdefault(num, []).append(c)
                break
    return [c for _, cols in sorted(seen.items()) for c in cols]


# Per-phase voltage / current / power-factor sub-columns share a meter's base name, so a
# substring meter match would wrongly catch them; these tokens identify and exclude them.
_METER_AUX_PATTERNS = (
    config.ACE_METER_VOLTAGE_PATTERNS
    + config.ACE_METER_CURRENT_PATTERNS
    + ["Power factor"]
)


def _meter_station_cols(df: pd.DataFrame, site_id: str = "") -> list:
    """Return the individual generation-meter station columns for a multi-meter site.

    These are exactly the raw meter columns the loader summed into the aggregate
    COL_METER_PRODUCTION_KW: for a site configured with meter_patterns, every column
    matching a pattern (with the per-phase V/I and power-factor sub-columns excluded).
    Auto-detected / single-meter sites have no per-station breakdown, so an empty list is
    returned and filter_meter_comms passes them through untouched.
    """
    site_cfg = config.SITE_CONFIGS.get(site_id)
    if site_cfg is None or site_cfg.meter_patterns is None:
        return []
    out = []
    for c in df.columns:
        lc = c.lower()
        if any(aux.lower() in lc for aux in _METER_AUX_PATTERNS):
            continue
        if any(pat.lower() in lc for pat in site_cfg.meter_patterns):
            out.append(c)
    return out


def filter_value_spikes(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop only the exact rows containing a physically-impossible channel value.

    A purely per-row, per-channel check against two absolute limits — nothing else.
    There is no context window, no neighbouring-row logic, and no site-specific
    behaviour: a row is dropped if and only if some inverter or meter channel's
    per-interval energy is above config.MAX_INTERVAL_KWH or below config.MIN_INTERVAL_KWH.

    Everything inside [MIN_INTERVAL_KWH, MAX_INTERVAL_KWH] is left untouched regardless of
    surrounding values — a valid reading immediately before a spike, a recovering reading
    immediately after one, and any real (even steep) production decline are all preserved.
    Only the offending spike rows themselves are removed.

    NaN channels never trigger a drop (NaN comparisons are False); they are handled by the
    other filters. The two config thresholds are the only knobs — a site at a different
    scale just adjusts them.
    """
    before = len(df)
    cols = _inverter_kw_cols(df) + [config.COL_METER_PRODUCTION_KW]
    interval_kwh = df[cols] * (config.INTERVAL_MINUTES / 60.0)
    impossible = (interval_kwh > config.MAX_INTERVAL_KWH) | (interval_kwh < config.MIN_INTERVAL_KWH)
    spike = impossible.any(axis=1)
    result = df[~spike].copy()
    dropped = before - len(result)
    site_tag = f" [{site_id}]" if site_id else ""
    print(f"  filter_value_spikes          {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}")
    return result


def filter_inverter_active(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop rows where any inverter reports zero or negative kW (nighttime or comms dropout).

    Zero catches nighttime; negative catches inverters that report −1 or similar sentinel
    values during communication failures.
    """
    before = len(df)
    raw_cols = _raw_inverter_cols(df, site_id)
    mask = (df[raw_cols] > 0).all(axis=1)
    result = df[mask].copy()
    dropped = before - len(result)
    site_tag = f" [{site_id}]" if site_id else ""
    print(f"  filter_inverter_active       {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}")
    return result


def filter_inverter_comms(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop intervals where one inverter's share of generation collapses relative to its
    baseline — a CT / communication dropout rather than a real production change.

    Premise: with every inverter online (filter_inverter_active has already run), the
    inverters split total generation in roughly stable proportions. A real production
    change (clouds, curtailment, irradiance) scales them together, so their *shares* stay
    about constant. If instead ONE inverter's reported output sags while the others carry
    on, its share drops well below normal — the signature of a comms/CT dropout — and the
    inverter total understates true generation, so the meter comparison for that interval
    is unreliable and the row is dropped.

    Each inverter's baseline share is the median of its per-interval share across the
    dataset (median is robust to the very dropouts being detected). A row whose total
    generation is at or above config.MIN_GEN_KW_FOR_SHARE_CHECK is dropped if any
    inverter's actual share deviates from its baseline by more than
    config.MAX_INVERTER_SHARE_DEVIATION (a fraction of that baseline). Rows below the
    generation floor are left alone (dawn/dusk shares are too noisy to judge), as are
    sites with fewer than two inverters (share is always 100%).
    """
    before = len(df)
    site_tag = f" [{site_id}]" if site_id else ""
    inv_cols = _inverter_kw_cols(df)

    if len(inv_cols) < 2 or before == 0:
        print(f"  filter_inverter_comms        {site_tag}: dropped {0:>6,} rows | remaining {len(df):,}")
        return df.copy()

    inv = df[inv_cols]
    total = inv.sum(axis=1)
    eligible = total >= config.MIN_GEN_KW_FOR_SHARE_CHECK
    shares = inv.div(total.where(total > 0), axis=0)

    # Baseline = each inverter's typical share, learned from eligible (non-dawn/dusk) rows.
    baseline = shares[eligible].median()
    rel_dev = shares.sub(baseline, axis=1).abs().div(baseline.where(baseline > 0), axis=1)
    flagged = eligible & (rel_dev > config.MAX_INVERTER_SHARE_DEVIATION).any(axis=1)

    result = df[~flagged].copy()
    dropped = before - len(result)
    print(f"  filter_inverter_comms        {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}")
    return result


def filter_meter_comms(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop intervals where one generation-meter station's share of total meter output
    collapses relative to its baseline — a faulted/dropped station making the aggregate
    meter reading untrustworthy.

    Per-station analogue of filter_inverter_comms, for sites whose production meter is split
    across multiple stations (e.g. PS1/PS2/PS3). Each station should hold a roughly stable
    share of total meter output: a real production change scales all stations together
    (shares ~constant), but a single station faulting or dropping out drives its share well
    off baseline while the others carry on. Each station's baseline share is the median of
    its per-interval share across the dataset (robust to the dropouts being detected). A row
    whose total meter output is at or above config.MIN_GEN_KW_FOR_SHARE_CHECK is dropped if
    any station's share deviates from its baseline by more than config.MAX_METER_SHARE_DEVIATION.

    Fully automatic and surgical: detection is purely per-interval, with no hardcoded dates
    or site-specific windows. If one station faults for two weeks while the others are fine,
    only those specific intervals are dropped; every interval where all stations agree with
    their baseline is preserved. Sites with fewer than two meter stations pass through
    untouched.
    """
    before = len(df)
    site_tag = f" [{site_id}]" if site_id else ""
    station_cols = _meter_station_cols(df, site_id)

    if len(station_cols) < 2 or before == 0:
        print(f"  filter_meter_comms           {site_tag}: dropped {0:>6,} rows | remaining {len(df):,}")
        return df.copy()

    stations = df[station_cols].apply(pd.to_numeric, errors="coerce")
    station_total = stations.sum(axis=1, skipna=True)
    shares = stations.div(station_total.where(station_total > 0), axis=0)

    # Eligibility uses the aggregate meter in kW so the floor matches MIN_GEN_KW_FOR_SHARE_CHECK's units.
    total_kw = pd.to_numeric(df[config.COL_METER_PRODUCTION_KW], errors="coerce")
    eligible = total_kw >= config.MIN_GEN_KW_FOR_SHARE_CHECK

    # Baseline = each station's typical share, learned from eligible (non-dawn/dusk) rows.
    baseline = shares[eligible].median()
    rel_dev = shares.sub(baseline, axis=1).abs().div(baseline.where(baseline > 0), axis=1)
    flagged = eligible & (rel_dev > config.MAX_METER_SHARE_DEVIATION).any(axis=1)

    result = df[~flagged].copy()
    dropped = before - len(result)
    print(f"  filter_meter_comms           {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}")
    return result


def filter_gross_outliers(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop rows whose per-interval efficiency is outside the sensor-sanity band
    [MIN_EFFICIENCY_PCT, MAX_EFFICIENCY_PCT].

    This is a wide band (50–150%) meant to remove only physically impossible readings
    (dead meter, miswired CT). Intervals reading slightly over 100% from meter/inverter
    timing jitter are legitimate and intentionally retained — they wash out under the
    energy-weighted roll-ups in the reporter.

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

    df = filter_value_spikes(df, site_id)
    df = filter_inverter_active(df, site_id)
    df = filter_inverter_comms(df, site_id)
    df = filter_meter_comms(df, site_id)
    df = filter_gross_outliers(df, site_id)

    rows_out = len(df)
    print(
        f"[cleaners]{site_tag} finished: {rows_out:,} rows remaining "
        f"({rows_in - rows_out:,} total dropped, "
        f"{(rows_in - rows_out) / rows_in * 100:.1f}%)\n"
    )
    return df
