"""Output layer: per-site cleaned CSVs and console tables."""

import calendar
import contextlib
import io
import re
from pathlib import Path
from typing import List

import pandas as pd
from tabulate import tabulate

from src.models import SiteRecord
from src import config


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


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
    """Return a summary dict using all clean intervals."""
    df = record.enriched_df
    ts = df[config.COL_TIMESTAMP]

    summary = {
        "site_name":          record.site_id,
        "total_raw_rows":     len(record.raw_df),
        "clean_rows":         len(df),
        "clean_row_pct":      round(len(df) / len(record.raw_df) * 100, 2),
        "avg_efficiency_pct": round(_weighted_efficiency_pct(df), 3),
        "min_efficiency_pct": round(df[config.COL_EFFICIENCY_PCT].min(), 3),
        "max_efficiency_pct": round(df[config.COL_EFFICIENCY_PCT].max(), 3),
        "avg_loss_delta_kw":     round(df[config.COL_LOSS_DELTA_KW].mean(), 3),
        "avg_loss_pct":          round(_weighted_loss_pct(df), 3),
        "total_energy_lost_kwh": round(df[config.COL_ENERGY_LOST_KWH].sum(), 1),
        "date_range_start":      ts.min().date() if not ts.empty else None,
        "date_range_end":        ts.max().date() if not ts.empty else None,
    }

    record.summary = summary
    return summary


def summarise_by_month(record: SiteRecord) -> pd.DataFrame:
    """Return a DataFrame with one row per (year, month) using all clean intervals."""
    df = record.enriched_df.copy()
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
    df = record.enriched_df
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
    df = record.enriched_df
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

    sink = io.StringIO()
    rows = []
    for pct in range(1, 6):
        threshold = pct / 100
        with contextlib.redirect_stdout(sink):
            df = _apply_phase_filter(record.raw_df, threshold)
            df = filter_inverter_active(df, record.site_id)
            df = filter_gross_outliers(df, record.site_id)
            enriched = run_all_calculations(df)
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
