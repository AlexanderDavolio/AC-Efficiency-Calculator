"""Cleaning filters — each accepts a DataFrame and returns a filtered DataFrame.

Filters are pure: they never mutate the input DataFrame.
All logic derives from raw sensor columns present in the DataFrame.
"""

import contextlib
import io
import re

import pandas as pd

from src import config
from src import calculator

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


def _phase_current_groups(df: pd.DataFrame) -> list:
    """Return per-meter-station phase-current column triples: [(stem, [IacA, IacB, IacC]), ...].

    A "station" is the common column-name stem shared by a set of per-phase AC current
    channels — e.g. "Generation Meter (PS1), IacA/IacB/IacC" all reduce to the stem
    "Generation Meter (PS1)". Columns are matched on config.ACE_METER_CURRENT_PATTERNS
    (["IacA", "IacB", "IacC"], ordered A/B/C); the stem is the column name with the phase
    token removed. Only stations exposing ALL THREE phases are returned, in A/B/C order; a
    station missing any phase column is skipped so filter_phase_current leaves it untouched.
    Works generically for any site that exports per-phase current — single- or multi-station.
    """
    pats = config.ACE_METER_CURRENT_PATTERNS  # ["IacA", "IacB", "IacC"] — A, B, C in order
    groups: dict = {}
    for c in df.columns:
        for idx, pat in enumerate(pats):
            if pat.lower() in c.lower():
                stem = re.sub(re.escape(pat), "", c, flags=re.IGNORECASE).strip().strip(",").strip()
                groups.setdefault(stem, {})[idx] = c
                break  # a column carries exactly one phase token
    return [
        (stem, [phases[i] for i in range(len(pats))])
        for stem, phases in groups.items()
        if len(phases) == len(pats)  # keep only stations with all three phases present
    ]


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
    """Drop intervals where one inverter reads negative during production (a hard fault) or
    where its share of generation collapses relative to its baseline — both comms / CT
    dropouts rather than a real production change.

    Two stages, in this order:

    1. Hard physical-fault check FIRST. While the site is producing (total inverter output
       at or above config.MIN_GEN_KW_FOR_SHARE_CHECK), no individual inverter string can
       read negative — a negative channel during production is physically impossible and is
       a comms/sensor fault, not a statistical deviation. Such rows are dropped outright AND
       excluded from the baseline below. This ordering matters: if a large fraction of
       intervals carry a negative channel, they would corrupt the median share and blind the
       deviation check, so the absolute impossibilities must be removed before any statistics
       are computed.

    2. Share-deviation check on what remains. With every inverter online the inverters split
       total generation in roughly stable proportions. A real production change (clouds,
       curtailment, irradiance) scales them together, so their *shares* stay about constant.
       If instead ONE inverter's reported output sags while the others carry on, its share
       drops well below normal — the signature of a comms/CT dropout — and the inverter total
       understates true generation, so the meter comparison for that interval is unreliable
       and the row is dropped. Each inverter's baseline share is the median of its per-interval
       share over the clean (non-faulted, non-dawn/dusk) rows. A producing row is dropped if
       any inverter's share deviates from its baseline by more than
       config.MAX_INVERTER_SHARE_DEVIATION (a fraction of that baseline).

    Rows below the generation floor are left alone (dawn/dusk shares are too noisy to judge),
    as are sites with fewer than two inverters (share is always 100%). No new config knob —
    the existing MIN_GEN_KW_FOR_SHARE_CHECK floor doubles as the "site is producing" gate for
    the negative-channel check.
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

    # Stage 1 — hard fault: any individual inverter negative while the site is producing.
    hard_fault = eligible & (inv < 0).any(axis=1)

    # Stage 2 — share deviation, with the baseline learned from clean rows only (eligible and
    # NOT hard-faulted) so a glut of negative intervals can't drag the median off true.
    shares = inv.div(total.where(total > 0), axis=0)
    baseline_rows = eligible & ~hard_fault
    baseline = shares[baseline_rows].median()
    rel_dev = shares.sub(baseline, axis=1).abs().div(baseline.where(baseline > 0), axis=1)
    deviation_flagged = baseline_rows & (rel_dev > config.MAX_INVERTER_SHARE_DEVIATION).any(axis=1)

    flagged = hard_fault | deviation_flagged
    result = df[~flagged].copy()
    dropped = before - len(result)
    n_hard = int(hard_fault.sum())
    extra = f" ({n_hard:,} hard-negative)" if n_hard else ""
    print(f"  filter_inverter_comms        {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}{extra}")
    return result


def filter_meter_comms(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Drop intervals where one generation-meter station reads negative during production
    (a hard fault) or where its share of total meter output collapses relative to its
    baseline — both make the aggregate meter reading untrustworthy.

    Per-station analogue of filter_inverter_comms, for sites whose production meter is split
    across multiple stations (e.g. PS1/PS2/PS3). Two stages, in this order:

    1. Hard physical-fault check FIRST. While the site is producing (aggregate meter at or
       above config.MIN_GEN_KW_FOR_SHARE_CHECK), no individual station can read negative — a
       negative station is a faulted CT / dropped meter, physically impossible during
       production, and must be treated as a hard fault rather than a statistical deviation.
       These rows are dropped outright AND excluded from the baseline below. The ordering is
       essential: this is exactly the April 2026 French's Landfill case, where 44% of station
       readings went negative; computing the median share over those rows dragged the baseline
       so far off true that the deviation check stopped catching the bad intervals. Removing
       the absolute impossibilities first restores a clean baseline.

    2. Share-deviation check on what remains. Each station should hold a roughly stable share
       of total meter output: a real production change scales all stations together (shares
       ~constant), but a single station faulting or dropping out drives its share well off
       baseline while the others carry on. Each station's baseline share is the median of its
       per-interval share over the clean (non-faulted, non-dawn/dusk) rows. A producing row is
       dropped if any station's share deviates from its baseline by more than
       config.MAX_METER_SHARE_DEVIATION.

    Fully automatic and surgical: detection is purely per-interval, with no hardcoded dates
    or site-specific windows, and no new config knob (the existing MIN_GEN_KW_FOR_SHARE_CHECK
    floor doubles as the "site is producing" gate for the negative-channel check). If one
    station faults for two weeks while the others are fine, only those specific intervals are
    dropped; every interval where all stations agree with their baseline is preserved. Sites
    with fewer than two meter stations pass through untouched.
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

    # Stage 1 — hard fault: any individual station negative while the site is producing.
    hard_fault = eligible & (stations < 0).any(axis=1)

    # Stage 2 — share deviation, with the baseline learned from clean rows only (eligible and
    # NOT hard-faulted) so the negative-station glut can't corrupt the median (the April 2026
    # failure mode above).
    baseline_rows = eligible & ~hard_fault
    baseline = shares[baseline_rows].median()
    rel_dev = shares.sub(baseline, axis=1).abs().div(baseline.where(baseline > 0), axis=1)
    deviation_flagged = baseline_rows & (rel_dev > config.MAX_METER_SHARE_DEVIATION).any(axis=1)

    flagged = hard_fault | deviation_flagged
    result = df[~flagged].copy()
    dropped = before - len(result)
    n_hard = int(hard_fault.sum())
    extra = f" ({n_hard:,} hard-negative)" if n_hard else ""
    print(f"  filter_meter_comms           {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}{extra}")
    return result


def filter_phase_current(df: pd.DataFrame, site_id: str = "", threshold: float = None,
                         daytime_only: bool = False) -> pd.DataFrame:
    """Drop intervals where one phase current of a meter station is out of line with the
    other two — a single-phase CT fault while the rest of the station reads normally.

    Per-PHASE analogue of filter_meter_comms, one level finer. Where filter_meter_comms
    compares each station's share of total meter ENERGY against a learned baseline, this
    compares the three per-phase AC currents (IacA/IacB/IacC) WITHIN each station against each
    other, at every interval. A healthy three-phase service carries near-equal current on all
    three legs, so the per-row median of the three is a robust reference; if one leg's CT
    drops out or saturates while the others hold, that phase swings far off the median. This
    catches faults the energy-share check misses: a single phase is only ~1/3 of a station, so
    a phase dropout moves the station's total energy too little to trip MAX_METER_SHARE_DEVIATION,
    yet shows up plainly leg-to-leg. (This is exactly the French's Landfill Apr 13-18 case —
    PS1 IacB flatlined near 257 A while IacA/IacC held around 1,550 A.)

    Two stages, mirroring filter_meter_comms, evaluated per station and unioned across stations:

    1. Hard physical-fault check FIRST: while the site is producing (aggregate meter at or
       above config.MIN_GEN_KW_FOR_SHARE_CHECK) no phase current can read negative — a negative
       leg is a faulted/dropped CT, not real current — so those rows are dropped outright.
    2. Median-deviation check on the remaining producing rows: per interval, take the median of
       a station's three phase currents and drop the row if any phase deviates from that median
       by more than the deviation threshold (a fraction of the median). The threshold defaults
       to config.MAX_PHASE_CURRENT_DEVIATION; run_all_filters passes an explicit `threshold` so
       it can raise the bar per site (the adaptive search) without changing the global default.

    The reference median is computed per ROW from the three phases (not a cross-row baseline),
    so it needs no warm-up and can't be corrupted by a run of bad intervals. Rows below the
    producing floor are left alone (phase currents are tiny and noisy at dawn/dusk). Stations
    missing any of the three phase columns are skipped; a site with no per-phase current data
    passes through untouched. Fully automatic and per-interval — no dates, no per-site config.

    `daytime_only` (set by run_all_filters for sites whose inverter telemetry is daytime-only)
    further restricts eligibility to intervals where the inverters are reporting, so nighttime
    phantom-load currents can't produce false imbalance flags.
    """
    before = len(df)
    site_tag = f" [{site_id}]" if site_id else ""
    groups = _phase_current_groups(df)

    if not groups or before == 0:
        print(f"  filter_phase_current         {site_tag}: dropped {0:>6,} rows | remaining {len(df):,}")
        return df.copy()

    # Deviation threshold: caller override (adaptive search) or the global default.
    thr = config.MAX_PHASE_CURRENT_DEVIATION if threshold is None else threshold

    total_kw = pd.to_numeric(df[config.COL_METER_PRODUCTION_KW], errors="coerce")
    eligible = total_kw >= config.MIN_GEN_KW_FOR_SHARE_CHECK

    # Daytime-only-telemetry sites: only judge phase balance on intervals where the inverters
    # are actually reporting, so nighttime phantom-load currents can't trip a false imbalance.
    if daytime_only:
        eligible = eligible & calculator.telemetry_reporting_mask(df)

    # Accumulate faults across every station's phase triple (a row is bad if ANY station is bad).
    hard_raw = pd.Series(False, index=df.index)   # any phase reads negative
    dev_raw = pd.Series(False, index=df.index)    # any phase off its station's per-row median
    for _stem, cols in groups:
        phases = df[cols].apply(pd.to_numeric, errors="coerce")
        hard_raw |= (phases < 0).any(axis=1)
        med = phases.median(axis=1)
        rel_dev = phases.sub(med, axis=0).abs().div(med.where(med > 0), axis=0)
        dev_raw |= (rel_dev > thr).any(axis=1)

    # Stage 1 hard faults, then stage 2 deviation on the producing rows that aren't already hard.
    hard_fault = eligible & hard_raw
    deviation_flagged = eligible & ~hard_fault & dev_raw
    flagged = hard_fault | deviation_flagged

    result = df[~flagged].copy()
    dropped = before - len(result)
    n_hard = int(hard_fault.sum())
    extra = f" ({n_hard:,} hard-negative)" if n_hard else ""
    print(f"  filter_phase_current         {site_tag}: dropped {dropped:>6,} rows | remaining {len(result):,}{extra}")
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


def _apply_filter_stack(df: pd.DataFrame, site_id: str, phase_threshold: float,
                        quiet: bool, daytime_only: bool = False) -> pd.DataFrame:
    """Run the six cleaning filters in order at one phase threshold and return the result.

    `quiet` suppresses each filter's own progress line — used while the adaptive search tries
    candidate thresholds, so only the final chosen run prints. `daytime_only` is forwarded to
    filter_phase_current to scope it to reporting intervals. The filters are pure, so this can
    be called repeatedly on the same input without side effects.
    """
    ctx = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
    with ctx:
        out = filter_value_spikes(df, site_id)
        out = filter_inverter_active(out, site_id)
        out = filter_inverter_comms(out, site_id)
        out = filter_meter_comms(out, site_id)
        out = filter_phase_current(out, site_id, threshold=phase_threshold, daytime_only=daytime_only)
        out = filter_gross_outliers(out, site_id)
    return out


def run_all_filters(df: pd.DataFrame, site_id: str = "") -> pd.DataFrame:
    """Apply all six filters in order, adapting the phase-current threshold per site.

    The phase-current threshold starts at config.MAX_PHASE_CURRENT_DEVIATION and, if the site
    yields fewer than config.MIN_GOOD_DAYS_ADAPTIVE good days, is raised by
    config.ADAPTIVE_THRESHOLD_STEP and the whole stack re-run — repeating until the site
    reaches the good-day floor or the threshold hits config.MAX_PHASE_CURRENT_DEVIATION_CEILING.
    The chosen threshold is used for this site's returned (final) frame. The adaptation is
    per-site and independent: it reads only this site's own good-day count, never another
    site's, and never any per-site-hardcoded value. Search metadata is attached to the returned
    frame's .attrs for the reporter (phase_threshold_used / _iterations / _hit_ceiling /
    _good_days / _ceiling). Every other filter is unchanged.
    """
    rows_in = len(df)
    site_tag = f" [{site_id}]" if site_id else ""

    start    = config.MAX_PHASE_CURRENT_DEVIATION
    step     = config.ADAPTIVE_THRESHOLD_STEP
    ceiling  = config.MAX_PHASE_CURRENT_DEVIATION_CEILING
    target   = config.MIN_GOOD_DAYS_ADAPTIVE
    has_phase = bool(_phase_current_groups(df))   # raising the bar is pointless without phases

    # Detect daytime-only inverter telemetry once, on the original frame (which still carries
    # the nighttime NULLs). When set, the good-day denominator and the phase filter are scoped
    # to reporting (daytime) intervals. Logged once so the adjustment is visible in output.
    daytime_only = calculator.is_daytime_only_telemetry(df)
    if daytime_only:
        pct = int(calculator.DAYTIME_ONLY_NIGHT_NULL_RATE * 100)
        print(f"[cleaners]{site_tag} WARNING: daytime-only inverter telemetry detected "
              f"(>{pct}% of nighttime intervals have no inverter data) — scoping good-day "
              f"denominator and phase-current filter to daytime (reporting) intervals")

    # ── Adaptive search (silent): step the phase threshold up until the site clears the
    #    good-day floor or we reach the ceiling. ────────────────────────────────────────────
    threshold = min(start, ceiling)
    iterations = 0
    good_days = 0
    while True:
        iterations += 1
        cleaned = _apply_filter_stack(df, site_id, threshold, quiet=True, daytime_only=daytime_only)
        good_days = calculator.count_good_days(df, cleaned.index, daytime_only=daytime_only)
        if good_days >= target or threshold >= ceiling or not has_phase:
            break
        threshold = min(round(threshold + step, 10), ceiling)
    hit_ceiling = has_phase and good_days < target  # below target only when stuck at the ceiling

    # ── Final run at the chosen threshold, with the usual per-filter logging. ──────────────
    print(f"\n[cleaners]{site_tag} starting: {rows_in:,} rows")
    cleaned = _apply_filter_stack(df, site_id, threshold, quiet=False, daytime_only=daytime_only)
    rows_out = len(cleaned)
    pct = (rows_in - rows_out) / rows_in * 100 if rows_in else 0.0
    print(
        f"[cleaners]{site_tag} finished: {rows_out:,} rows remaining "
        f"({rows_in - rows_out:,} total dropped, {pct:.1f}%)"
    )
    if hit_ceiling:
        print(f"[cleaners]{site_tag} phase threshold reached ceiling {ceiling:g} "
              f"({iterations} iterations) — only {good_days} good days (target {target})")
    elif iterations > 1:
        print(f"[cleaners]{site_tag} phase threshold auto-adjusted to {threshold:g} "
              f"({iterations} iterations) — {good_days} good days")
    else:
        print(f"[cleaners]{site_tag} phase threshold {threshold:g} "
              f"(1 iteration) — {good_days} good days")
    print()

    cleaned.attrs["phase_threshold_used"]        = threshold
    cleaned.attrs["phase_threshold_iterations"]  = iterations
    cleaned.attrs["phase_threshold_hit_ceiling"] = hit_ceiling
    cleaned.attrs["phase_threshold_good_days"]   = good_days
    cleaned.attrs["phase_threshold_ceiling"]     = ceiling
    return cleaned
