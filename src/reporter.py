"""Output layer: per-site cleaned CSVs and console tables."""

import calendar
import contextlib
import io
import re
import textwrap
from pathlib import Path
from typing import List

import pandas as pd
from tabulate import tabulate

from src.models import SiteRecord
from src import config


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


# ── Months kept out of reported efficiency ─────────────────────────────────────
# Two reasons a month is excluded from every reported efficiency figure (monthly table,
# OVERALL, time-of-day, inverter split) yet kept in the raw data and cleaned CSV:
#   1. config: a SITE_CONFIGS excluded_months entry (e.g. a confirmed CT fault)
#   2. anomaly: the month's efficiency is statistically far below the site's own history
# Both are surfaced in the Data Gaps section. The data frames flow through three layers:
#   _baseline_df   — config exclusions removed (the basis for anomaly detection)
#   _reportable_df — config AND anomaly exclusions removed (basis for all reported figures)
# Anomaly detection runs on _baseline_df (NOT _reportable_df) to avoid a circular dependency.

def _excluded_periods(site_id: str) -> set:
    """Monthly pd.Periods a site's config explicitly excludes (SITE_CONFIGS.excluded_months)."""
    cfg = config.SITE_CONFIGS.get(site_id)
    months = getattr(cfg, "excluded_months", None) if cfg else None
    if not months:
        return set()
    return {pd.Period(m, freq="M") for m in months}


def _drop_periods(df: pd.DataFrame, periods: set) -> pd.DataFrame:
    """Return df without rows whose timestamp falls in any of the given monthly Periods."""
    if not periods:
        return df
    per = pd.to_datetime(df[config.COL_TIMESTAMP], errors="coerce").dt.to_period("M")
    return df[~per.isin(periods)]


def _monthly_weighted_efficiency(df: pd.DataFrame) -> pd.Series:
    """Energy-weighted efficiency (%) per month for months meeting the minimum-interval
    threshold, indexed by monthly Period. Matches the monthly table's avg_efficiency_pct,
    so anomaly stats are computed on exactly the figures that would be reported."""
    d = df[df[config.COL_EFFICIENCY_PCT].notna()]
    if d.empty:
        return pd.Series(dtype=float)
    per = pd.to_datetime(d[config.COL_TIMESTAMP], errors="coerce").dt.to_period("M")
    grp = d.groupby(per)
    counts = grp[config.COL_EFFICIENCY_PCT].count()
    meter = grp[config.COL_METER_PRODUCTION_KW].sum()
    inv = grp[config.COL_TOTAL_INVERTER_KW].sum()
    eff = 100.0 * meter / inv.where(inv > 0)
    return eff[counts >= config.MIN_CLEAN_INTERVALS_PER_MONTH]


def _anomalous_periods(record: SiteRecord) -> set:
    """Monthly Periods whose reported efficiency is statistically anomalous — more than
    config.ANOMALY_STD_THRESHOLD standard deviations BELOW the site's own median monthly
    efficiency. Computed on _baseline_df (config exclusions removed). Returns empty when
    there are fewer than config.ANOMALY_MIN_MONTHS months or no spread to judge."""
    eff = _monthly_weighted_efficiency(_baseline_df(record)).dropna()
    if len(eff) < config.ANOMALY_MIN_MONTHS:
        return set()
    median = eff.median()
    std = eff.std()  # sample std (ddof=1)
    if pd.isna(std) or std == 0:
        return set()
    cutoff = median - config.ANOMALY_STD_THRESHOLD * std
    return set(eff.index[eff < cutoff])


def _nonreportable_periods(record: SiteRecord) -> set:
    """All months kept out of reported efficiency: config-excluded ∪ statistically anomalous."""
    return _excluded_periods(record.site_id) | _anomalous_periods(record)


def _baseline_df(record: SiteRecord) -> pd.DataFrame:
    """enriched_df with config-excluded months removed — basis for anomaly detection."""
    return _drop_periods(record.enriched_df, _excluded_periods(record.site_id))


def _reportable_df(record: SiteRecord) -> pd.DataFrame:
    """enriched_df with config-excluded AND statistically-anomalous months removed — the
    basis for ALL reported efficiency figures."""
    return _drop_periods(record.enriched_df, _nonreportable_periods(record))


# ── Energy-weighted roll-ups ──────────────────────────────────────────────────
# Reported efficiency and loss % are ALWAYS energy-weighted — the per-interval
# EFFICIENCY_PCT column exists only for diagnostics (min/max) and must never be
# averaged with .mean() for a headline figure. Weighting each interval equally
# would over-count low-energy dawn/dusk intervals; weighting by energy is the only
# physically correct roll-up. The interval-duration factor cancels, so summing the
# kW columns is equivalent to summing kWh.

def _weighted_sums(df: pd.DataFrame) -> tuple:
    """Return (sum_meter_kw, sum_inverter_kw) over intervals where efficiency is defined.

    EFFICIENCY_PCT is NaN exactly when the meter reading is missing or inverter total
    is non-positive, so masking on it pairs the two sums correctly. Summing each column
    independently would let a NaN-meter interval still contribute to the inverter sum,
    inflating the denominator and deflating the reported efficiency.
    """
    valid = df[df[config.COL_EFFICIENCY_PCT].notna()]
    return (
        valid[config.COL_METER_PRODUCTION_KW].sum(),
        valid[config.COL_TOTAL_INVERTER_KW].sum(),
    )


def _weighted_efficiency_pct(df: pd.DataFrame) -> float:
    """Energy-weighted efficiency: 100 * sum(meter_kw) / sum(inverter_kw)."""
    meter, inv_total = _weighted_sums(df)
    if inv_total <= 0:
        return float("nan")
    return 100.0 * meter / inv_total


def _weighted_loss_pct(df: pd.DataFrame) -> float:
    """Energy-weighted loss %: 100 * (sum(inverter_kw) - sum(meter_kw)) / sum(inverter_kw)."""
    meter, inv_total = _weighted_sums(df)
    if inv_total <= 0:
        return float("nan")
    return 100.0 * (inv_total - meter) / inv_total


# ── Per-site summaries ────────────────────────────────────────────────────────

def summarise_site(record: SiteRecord) -> dict:
    """Return a summary dict. Efficiency/loss figures use only reportable months (config
    exclusions removed); row counts and the date range reflect all clean data."""
    full = record.enriched_df               # all clean intervals — counts and date range
    rep = _reportable_df(record)            # excludes untrustworthy months — efficiency
    ts = full[config.COL_TIMESTAMP]

    summary = {
        "site_name":          record.site_id,
        "total_raw_rows":     len(record.raw_df),
        "clean_rows":         len(full),
        "clean_row_pct":      round(len(full) / len(record.raw_df) * 100, 2),
        "avg_efficiency_pct": round(_weighted_efficiency_pct(rep), 3),
        "min_efficiency_pct": round(rep[config.COL_EFFICIENCY_PCT].min(), 3),
        "max_efficiency_pct": round(rep[config.COL_EFFICIENCY_PCT].max(), 3),
        "avg_loss_delta_kw":     round(rep[config.COL_LOSS_DELTA_KW].mean(), 3),
        "avg_loss_pct":          round(_weighted_loss_pct(rep), 3),
        "total_energy_lost_kwh": round(rep[config.COL_ENERGY_LOST_KWH].sum(), 1),
        "date_range_start":      ts.min().date() if not ts.empty else None,
        "date_range_end":        ts.max().date() if not ts.empty else None,
    }

    record.summary = summary
    return summary


def summarise_by_month(record: SiteRecord) -> pd.DataFrame:
    """Return a DataFrame with one row per (year, month) using reportable clean intervals
    (config-excluded months removed; months below the minimum-interval threshold dropped)."""
    df = _reportable_df(record).copy()
    # Restrict to intervals where efficiency is defined (meter present, inverter > 0)
    # so the energy-weighted meter/inverter sums below stay paired. See _weighted_sums.
    df = df[df[config.COL_EFFICIENCY_PCT].notna()]
    ts = pd.to_datetime(df[config.COL_TIMESTAMP])
    df["_year"]  = ts.dt.year
    df["_month"] = ts.dt.month

    agg = (
        df.groupby(["_year", "_month"])
        .agg(
            row_count=(config.COL_EFFICIENCY_PCT, "count"),
            min_efficiency_pct=(config.COL_EFFICIENCY_PCT, "min"),
            max_efficiency_pct=(config.COL_EFFICIENCY_PCT, "max"),
            avg_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "mean"),
            total_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "sum"),
            total_energy_lost_kwh=(config.COL_ENERGY_LOST_KWH, "sum"),
            _sum_meter_kw=(config.COL_METER_PRODUCTION_KW, "sum"),
            _sum_inverter_kw=(config.COL_TOTAL_INVERTER_KW, "sum"),
        )
        .reset_index()
        .sort_values(["_year", "_month"])
        .reset_index(drop=True)
    )

    # Drop months with too few clean intervals to yield a meaningful figure — they are
    # surfaced in the Data Gaps section instead (see summarise_gap_months). This keeps a
    # handful of timing-jittered or CT-dropout-driven rows from being reported as a number.
    agg = agg[agg["row_count"] >= config.MIN_CLEAN_INTERVALS_PER_MONTH].reset_index(drop=True)

    # Energy-weighted efficiency and loss % per month (not a mean of per-interval ratios).
    inv_pos = agg["_sum_inverter_kw"].where(agg["_sum_inverter_kw"] > 0)
    agg["avg_efficiency_pct"] = 100.0 * agg["_sum_meter_kw"] / inv_pos
    agg["avg_loss_pct"] = 100.0 * (agg["_sum_inverter_kw"] - agg["_sum_meter_kw"]) / inv_pos

    if agg.empty:
        return pd.DataFrame(columns=["Month", "Valid Readings", "Average Efficiency (%)",
                                     "Min Efficiency (%)", "Max Efficiency (%)",
                                     "Average Power Loss (kW)", "Loss % of Output",
                                     "Total Power Loss (kW)", "Total Energy Lost (kWh)"])

    float_cols = ["avg_efficiency_pct", "min_efficiency_pct", "max_efficiency_pct",
                  "avg_loss_delta_kw", "total_loss_delta_kw"]
    agg[float_cols] = agg[float_cols].round(3)
    agg["avg_loss_pct"] = agg["avg_loss_pct"].round(3)
    agg["total_energy_lost_kwh"] = agg["total_energy_lost_kwh"].round(1)

    month_labels = agg.apply(
        lambda r: f"{calendar.month_abbr[int(r['_month'])]} {int(r['_year'])}", axis=1
    )

    return pd.DataFrame({
        "Month":                    month_labels,
        "Valid Readings":           agg["row_count"],
        "Average Efficiency (%)":   agg["avg_efficiency_pct"],
        "Min Efficiency (%)":       agg["min_efficiency_pct"],
        "Max Efficiency (%)":       agg["max_efficiency_pct"],
        "Average Power Loss (kW)":  agg["avg_loss_delta_kw"],
        "Loss % of Output":         agg["avg_loss_pct"],
        "Total Power Loss (kW)":    agg["total_loss_delta_kw"],
        "Total Energy Lost (kWh)":  agg["total_energy_lost_kwh"],
    })


def summarise_by_time_bucket(record: SiteRecord) -> pd.DataFrame:
    """Return a DataFrame with one row per time bucket (excluding 'Other')."""
    df = _reportable_df(record)
    # Keep only efficiency-defined intervals so the energy-weighted sums stay paired.
    df = df[(df[config.COL_TIME_BUCKET] != "Other") & df[config.COL_EFFICIENCY_PCT].notna()]

    agg = (
        df.groupby(config.COL_TIME_BUCKET)
        .agg(
            row_count=(config.COL_EFFICIENCY_PCT, "count"),
            avg_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "mean"),
            _sum_meter_kw=(config.COL_METER_PRODUCTION_KW, "sum"),
            _sum_inverter_kw=(config.COL_TOTAL_INVERTER_KW, "sum"),
        )
        .reset_index()
        .rename(columns={config.COL_TIME_BUCKET: "time_bucket"})
    )

    # Energy-weighted efficiency per time bucket (not a mean of per-interval ratios).
    inv_pos = agg["_sum_inverter_kw"].where(agg["_sum_inverter_kw"] > 0)
    agg["avg_efficiency_pct"] = 100.0 * agg["_sum_meter_kw"] / inv_pos

    float_cols = ["avg_efficiency_pct", "avg_loss_delta_kw"]
    agg[float_cols] = agg[float_cols].round(3)

    bucket_order = ["Morning", "Peak", "Afternoon"]
    agg["time_bucket"] = pd.Categorical(agg["time_bucket"], categories=bucket_order, ordered=True)
    agg = agg.sort_values("time_bucket").reset_index(drop=True)

    label_map = {
        "Morning":   "Morning (6am–9am)",
        "Peak":      "Peak (10am–1pm)",
        "Afternoon": "Afternoon (2pm–5pm)",
    }
    agg["time_bucket"] = agg["time_bucket"].map(label_map)

    return pd.DataFrame({
        "Time Period":             agg["time_bucket"],
        "Valid Readings":          agg["row_count"],
        "Average Efficiency (%)":  agg["avg_efficiency_pct"],
        "Average Power Loss (kW)": agg["avg_loss_delta_kw"],
    })


def summarise_inverters(record: SiteRecord) -> dict:
    """Return a dict of inverter power share metrics.

    Equal share = 100 / n_inverters. A site is flagged if any inverter deviates
    more than INVERTER_IMBALANCE_TOLERANCE_PP percentage points from that equal share.
    """
    df = _reportable_df(record)
    valid = df[df[config.COL_TOTAL_INVERTER_KW] > 0]

    inv_cols = sorted(
        [c for c in df.columns if re.match(r"^Inverter \d+ AC kW$", c)],
        key=lambda c: int(re.search(r"\d+", c).group()),
    )

    n = len(inv_cols)
    equal_share = 100.0 / n

    row = {"Site": record.site_id}
    shares = []
    for i, col in enumerate(inv_cols, start=1):
        share = round((valid[col] / valid[config.COL_TOTAL_INVERTER_KW] * 100).mean(), 3)
        row[f"Inverter {i} Power Share (%)"] = share
        shares.append(share)

    imbalanced = any(abs(s - equal_share) > config.INVERTER_IMBALANCE_TOLERANCE_PP for s in shares)
    row["Notes"] = "Imbalance detected" if imbalanced else ""
    return row


def summarise_gap_months(record: SiteRecord) -> list:
    """Return month-level data gaps: months present in the raw data with meter and/or
    inverter activity recorded, but too few clean intervals to report an efficiency.

    A month is reported in the monthly table only if it has at least
    config.MIN_CLEAN_INTERVALS_PER_MONTH clean (efficiency-defined) intervals. Every other
    month that still had recorded data is surfaced here instead of silently vanishing, so
    "no data" is distinguishable from "data present but dropped/too sparse to trust". The
    filter logic is not touched; this only inspects what survived.

    Reasons (checked in priority order; a config exclusion wins over an anomaly, which
    wins over the count-based reasons):
      - "CT issue - meter readings unreliable" — month listed in the site's
        SITE_CONFIGS excluded_months; data exists but is not trusted for efficiency
      - "efficiency anomaly - possible meter or instrumentation fault" — the month's
        efficiency is statistically far below the site's own median (see _anomalous_periods)
      - "insufficient clean intervals" — some clean data, but below the monthly minimum
        (e.g. a handful of timing-jittered rows, or CT/comms dropouts gutting the month)
      - "incomplete inverter data" — meter and generation present, but never all inverters
        reporting at once, so zero intervals survived filter_inverter_active
      - "no inverter telemetry" / "no meter data" — only one side recorded that month

    Returns a list of dicts (chronological) with keys: month, raw_rows, meter_rows,
    gen_rows, clean_rows, reason. Months with no recorded data at all (pre-commissioning,
    true outages) are not reported — they are genuinely absent, not gaps.
    """
    raw = record.raw_df
    if raw is None or len(raw) == 0:
        return []

    period = pd.to_datetime(raw[config.COL_TIMESTAMP], errors="coerce").dt.to_period("M")
    inv_cols = [c for c in raw.columns if re.match(r"^Inverter \d+ AC kW$", c)]
    inv_total = raw[inv_cols].sum(axis=1) if inv_cols else pd.Series(0.0, index=raw.index)
    if config.COL_METER_PRODUCTION_KW in raw.columns:
        meter = pd.to_numeric(raw[config.COL_METER_PRODUCTION_KW], errors="coerce")
    else:
        meter = pd.Series(0.0, index=raw.index)

    # Count clean, efficiency-defined intervals per month — the same basis as the monthly
    # table's row count. A month is "reported" iff this reaches the minimum threshold.
    clean = record.enriched_df
    clean = clean[clean[config.COL_EFFICIENCY_PCT].notna()]
    clean_counts = (
        pd.to_datetime(clean[config.COL_TIMESTAMP], errors="coerce")
        .dt.to_period("M").value_counts()
    )
    min_clean = config.MIN_CLEAN_INTERVALS_PER_MONTH
    excluded = _excluded_periods(record.site_id)
    anomalous = _anomalous_periods(record)

    work = pd.DataFrame({"period": period, "meter": meter, "gen": inv_total})
    gaps = []
    for p, sub in work.groupby("period"):  # groupby drops NaT and sorts chronologically
        clean_count = int(clean_counts.get(p, 0))
        meter_rows = int((sub["meter"] > 0).sum())
        gen_rows = int((sub["gen"] > 0).sum())
        if p in excluded:
            reason = "CT issue - meter readings unreliable"  # manager-confirmed; overrides all
        elif p in anomalous:
            reason = "efficiency anomaly - possible meter or instrumentation fault"
        elif clean_count >= min_clean:
            continue  # enough clean data — reported as a real month in the table
        elif meter_rows == 0 and gen_rows == 0:
            continue  # genuinely no data this month — absent, not a gap
        elif clean_count > 0:
            reason = "insufficient clean intervals"  # below the monthly minimum to trust a number
        elif gen_rows == 0:
            reason = "no inverter telemetry"         # meter recording, inverters never reported
        elif meter_rows == 0:
            reason = "no meter data"                 # inverters generating, meter not recording
        else:
            reason = "incomplete inverter data"      # both present, but not all inverters at once
        gaps.append({
            "month":      f"{calendar.month_abbr[p.month]} {p.year}",
            "raw_rows":   int(len(sub)),
            "meter_rows": meter_rows,
            "gen_rows":   gen_rows,
            "clean_rows": clean_count,
            "reason":     reason,
        })
    return gaps


# ── CSV output ────────────────────────────────────────────────────────────────

def write_cleaned_csv(record: SiteRecord, output_dir: Path = OUTPUT_DIR) -> Path:
    """Write enriched_df to output/<site_id>_cleaned.csv and return the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record.site_id}_cleaned.csv"
    record.enriched_df.to_csv(path, index=False)
    print(f"[reporter] wrote cleaned CSV   : {path}")
    return path


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _phase_sensitivity(record: SiteRecord) -> list:
    """Return avg efficiency for phase thresholds 1–5%, running the full filter+calc pipeline.

    The phase imbalance filter is reproduced inline here so cleaners.py stays unchanged.
    """
    from src.cleaners import filter_inverter_active, filter_gross_outliers
    from src.calculator import run_all_calculations

    def _apply_phase_filter(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
        lower_map = {c.lower(): c for c in df.columns}
        flag = pd.Series(False, index=df.index)
        for patterns in (config.ACE_METER_VOLTAGE_PATTERNS, config.ACE_METER_CURRENT_PATTERNS):
            present = []
            for pat in patterns:
                match = next((orig for lc, orig in lower_map.items() if pat.lower() in lc), None)
                if match:
                    present.append(match)
            if len(present) < 2:
                continue
            vals = df[present]
            mean = vals.mean(axis=1)
            devs = vals.sub(mean, axis=0).abs().div(mean.where(mean != 0), axis=0)
            flag |= (devs > threshold).any(axis=1).fillna(False)
        return df[~flag].copy()

    nonreportable = _nonreportable_periods(record)
    sink = io.StringIO()
    rows = []
    for pct in range(1, 6):
        threshold = pct / 100
        with contextlib.redirect_stdout(sink):
            df = _apply_phase_filter(record.raw_df, threshold)
            df = filter_inverter_active(df, record.site_id)
            df = filter_gross_outliers(df, record.site_id)
            enriched = run_all_calculations(df)
        if nonreportable:  # keep the sensitivity figures consistent with the headline numbers
            per = pd.to_datetime(enriched[config.COL_TIMESTAMP], errors="coerce").dt.to_period("M")
            enriched = enriched[~per.isin(nonreportable)]
        rows.append({
            "threshold_pct":     pct,
            "rows_kept":         len(df),
            "avg_efficiency_pct": round(_weighted_efficiency_pct(enriched), 2),
        })
    return rows


def _print_sensitivity_table(all_sens: dict) -> None:
    """Print a threshold × site sensitivity table using tabulate.

    all_sens: {site_name: [{"threshold_pct": int, "avg_efficiency_pct": float}, ...]}
    """
    sites = list(all_sens.keys())
    rows = []
    for pct in range(1, 6):
        row = [f"{pct}%"]
        for site in sites:
            entry = next((r for r in all_sens[site] if r["threshold_pct"] == pct), None)
            eff = entry["avg_efficiency_pct"] if entry else float("nan")
            row.append(f"{eff:.2f}%" if pd.notna(eff) else "nan%")
        rows.append(row)
    headers = ["Threshold"] + sites
    print(f"  Phase Imbalance Sensitivity (All Sites)")
    print(tabulate(rows, headers=headers, tablefmt="simple", colalign=("right",) + ("right",) * len(sites)))
    print()


def _print_summary_table(summaries: list) -> None:
    """Print a one-row-per-site summary table."""
    print(f"\n{'='*86}")
    print(f"  Summary")
    print(f"{'='*86}")
    print(f"  {'Site':<22}  {'Date Range':<23}  {'Avg Efficiency':>16}  {'Total Energy Lost':>20}")
    print(f"  {'-'*22}  {'-'*23}  {'-'*16}  {'-'*20}")
    for s in summaries:
        date_range   = (
            f"{s['date_range_start']} – {s['date_range_end']}"
            if s["date_range_start"] else "N/A"
        )
        avg_eff      = s["avg_efficiency_pct"]
        total_energy = s["total_energy_lost_kwh"]
        eff_str    = f"{avg_eff:>15.2f}%"         if pd.notna(avg_eff)      else f"{'N/A':>15} "
        energy_str = f"{total_energy:>16.1f} kWh" if pd.notna(total_energy) else f"{'N/A':>16}    "
        print(f"  {s['site_name']:<22}  {date_range:<23}  {eff_str}  {energy_str}")
    print(f"{'='*86}\n")


def run_all_reports(records: List[SiteRecord], output_dir: Path = OUTPUT_DIR) -> None:
    """Print per-site tables, a cross-site summary, and write cleaned CSVs."""
    if not records:
        print("[reporter] No records loaded — nothing to report.")
        return

    all_summaries = []
    all_sens: dict = {}

    for r in records:
        summary = summarise_site(r)
        all_summaries.append(summary)
        write_cleaned_csv(r, output_dir)

        monthly = summarise_by_month(r)
        inv_row = summarise_inverters(r)
        gaps = summarise_gap_months(r)

        # ── Monthly efficiency ────────────────────────────────────────────────
        print(f"\n{'='*74}")
        print(f"  {summary['site_name']}")
        print(f"{'='*74}")
        print(f"  {'Month':<12}  {'Avg Efficiency':>16}  {'Loss % of Output':>18}  {'Total Energy Lost':>20}")
        print(f"  {'-'*12}  {'-'*16}  {'-'*18}  {'-'*20}")
        for _, row in monthly.iterrows():
            eff      = row["Average Efficiency (%)"]
            loss_pct = row["Loss % of Output"]
            energy   = row["Total Energy Lost (kWh)"]
            eff_str      = f"{eff:>15.2f}%"      if pd.notna(eff)      else f"{'N/A':>15} "
            loss_pct_str = f"{loss_pct:>15.2f}%"  if pd.notna(loss_pct) else f"{'N/A':>15} "
            energy_str   = f"{energy:>16.1f} kWh" if pd.notna(energy)   else f"{'N/A':>16}    "
            print(f"  {row['Month']:<12}  {eff_str}  {loss_pct_str}  {energy_str}")
        print(f"{'='*74}")
        avg_eff      = summary["avg_efficiency_pct"]
        avg_loss_pct = summary["avg_loss_pct"]
        total_energy = summary["total_energy_lost_kwh"]
        eff_str      = f"{avg_eff:>15.2f}%"         if pd.notna(avg_eff)      else f"{'N/A':>15} "
        loss_pct_str = f"{avg_loss_pct:>15.2f}%"    if pd.notna(avg_loss_pct) else f"{'N/A':>15} "
        energy_str   = f"{total_energy:>16.1f} kWh" if pd.notna(total_energy) else f"{'N/A':>16}    "
        print(f"  {'OVERALL':<12}  {eff_str}  {loss_pct_str}  {energy_str}")
        print(f"{'='*74}\n")

        # ── Data gaps (recorded data, but too few clean intervals to report) ──
        if gaps:
            print(
                f"  Data Gaps  (not reported above: < {config.MIN_CLEAN_INTERVALS_PER_MONTH} "
                f"clean intervals, config-excluded, or a statistical efficiency anomaly)"
            )
            print(f"  {'-'*86}")
            for g in gaps:
                print(
                    f"  {g['month']:<10}  clean: {g['clean_rows']:>5,} | meter: {g['meter_rows']:>5,} | "
                    f"gen: {g['gen_rows']:>5,}   {g['reason']}"
                )
            print()

        # ── Curated site notes (config.SITE_NOTES) ────────────────────────────
        site_notes = config.SITE_NOTES.get(r.site_id, [])
        if site_notes:
            print(f"  Notes")
            print(f"  {'-'*78}")
            for note in site_notes:
                print(textwrap.fill(note, width=78, initial_indent="  * ", subsequent_indent="    "))
            print()

        # ── Inverter power split ──────────────────────────────────────────────
        inv_keys = [k for k in inv_row if k.startswith("Inverter")]
        if inv_keys:
            print(f"  Inverter Power Split")
            print(f"  {'-'*42}")
            for k in inv_keys:
                print(f"  {k:<32}  {inv_row[k]:>6.2f}%")
            if inv_row.get("Notes"):
                print(f"  ** {inv_row['Notes']}")
            print()

        all_sens[r.site_id] = _phase_sensitivity(r)

    _print_summary_table(all_summaries)
    _print_sensitivity_table(all_sens)
