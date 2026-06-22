"""Cumulative-register detection and conversion.

Most DAS exports deliver each energy channel as per-interval energy (the kWh accrued
during a single interval). Some sites instead export cumulative *lifetime* registers —
a running kWh total that only ever counts up. Multiplying a cumulative register by the
kWh -> kW factor produces meaningless multi-million-kW values and a completely broken
efficiency calculation.

This module detects cumulative columns generically (no per-site config) and converts
them back to per-interval energy by differencing consecutive readings in timestamp
order. Counter resets / register rollovers -- where the running total jumps sharply
negative -- are clamped to zero so a rollover contributes no energy instead of a huge
negative spike.

Both excel_loader and csv_loader pass every raw energy column (meter and per-inverter)
through to_interval_if_cumulative() immediately before the kWh -> kW conversion, so any
site that exports cumulative registers is handled automatically with no config change.
"""

import pandas as pd

from src import config


def to_interval_if_cumulative(
    series: pd.Series,
    timestamp: pd.Series,
    col_name: str,
    site_id: str = "",
) -> pd.Series:
    """Return per-interval energy for one raw energy column.

    If the column is already per-interval energy it is returned unchanged. If it is a
    cumulative lifetime register it is differenced into per-interval energy and a
    warning is logged.

    Detection (generic, no per-site config): order the values chronologically and look
    at consecutive deltas. A cumulative register only ever counts up, so it is
    non-decreasing across nearly every step (apart from occasional resets/rollovers);
    per-interval energy falls every afternoon and is non-decreasing only ~75-85% of the
    time. A column is treated as cumulative when it is non-decreasing for at least
    config.CUMULATIVE_NONDECREASING_FRAC of its steps AND strictly rises at least
    config.CUMULATIVE_MIN_RISE_FRAC of the time. The rise check excludes dead all-zero
    or constant columns, which are trivially non-decreasing.

    Conversion: diff() in timestamp order; the first interval (no predecessor) becomes
    NaN; any negative delta (counter reset / register rollover) is clamped to 0 so a
    rollover yields zero interval energy rather than a massive negative number.

    `timestamp` is used only to order the rows -- the source DataFrame is not yet sorted
    by timestamp at the point this runs -- and the result is realigned to the original
    row order before being returned, so callers can use it as a drop-in for the raw
    series.
    """
    # Order chronologically for differencing; NaT timestamps sort to the end.
    order = timestamp.sort_values(kind="stable").index
    ordered = series.reindex(order)

    valid = ordered.dropna()
    if len(valid) < config.CUMULATIVE_MIN_ROWS:
        return series  # too little data to judge monotonicity -- assume per-interval

    diffs = valid.diff().dropna()
    if diffs.empty:
        return series

    nondecreasing_frac = float((diffs >= 0).mean())
    rise_frac = float((diffs > 0).mean())
    if not (
        nondecreasing_frac >= config.CUMULATIVE_NONDECREASING_FRAC
        and rise_frac >= config.CUMULATIVE_MIN_RISE_FRAC
    ):
        return series  # looks like per-interval energy -- leave it alone

    # ── Cumulative register -> per-interval energy ────────────────────────────
    delta = ordered.diff()                   # first row -> NaN (no predecessor)
    negative = delta < 0                     # counter reset / register rollover
    n_rollovers = int(negative.sum())
    delta = delta.mask(negative, 0.0)        # rollover -> 0 energy, not a huge negative
    converted = delta.reindex(series.index)  # restore the DataFrame's original row order

    span = float(valid.iloc[-1] - valid.iloc[0])
    site_tag = f"[{site_id}] " if site_id else ""
    print(
        f"[cumulative] WARNING: {site_tag}column '{col_name}' detected as a cumulative "
        f"register (non-decreasing across {nondecreasing_frac * 100:.1f}% of steps, "
        f"lifetime span ~ {span:,.0f} kWh, {n_rollovers} rollover/reset event(s)); "
        f"differenced to per-interval energy."
    )
    return converted
