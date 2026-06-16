"""Output layer: per-site cleaned CSVs and console tables."""

import calendar
import re
from pathlib import Path
from typing import List

import pandas as pd

from src.models import SiteRecord
from src import config


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

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
        "avg_efficiency_pct": round(df[config.COL_EFFICIENCY_PCT].mean(), 3),
        "min_efficiency_pct": round(df[config.COL_EFFICIENCY_PCT].min(), 3),
        "max_efficiency_pct": round(df[config.COL_EFFICIENCY_PCT].max(), 3),
        "avg_loss_delta_kw":     round(df[config.COL_LOSS_DELTA_KW].mean(), 3),
        "avg_loss_pct":          round(df[config.COL_LOSS_PCT].mean(), 3),
        "total_energy_lost_kwh": round(df[config.COL_ENERGY_LOST_KWH].sum(), 1),
        "date_range_start":      ts.min().date() if not ts.empty else None,
        "date_range_end":        ts.max().date() if not ts.empty else None,
    }

    record.summary = summary
    return summary


def summarise_by_month(record: SiteRecord) -> pd.DataFrame:
    """Return a DataFrame with one row per (year, month) using all clean intervals."""
    df = record.enriched_df.copy()
    ts = pd.to_datetime(df[config.COL_TIMESTAMP])
    df["_year"]  = ts.dt.year
    df["_month"] = ts.dt.month

    agg = (
        df.groupby(["_year", "_month"])
        .agg(
            row_count=(config.COL_EFFICIENCY_PCT, "count"),
            avg_efficiency_pct=(config.COL_EFFICIENCY_PCT, "mean"),
            min_efficiency_pct=(config.COL_EFFICIENCY_PCT, "min"),
            max_efficiency_pct=(config.COL_EFFICIENCY_PCT, "max"),
            avg_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "mean"),
            avg_loss_pct=(config.COL_LOSS_PCT, "mean"),
            total_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "sum"),
            total_energy_lost_kwh=(config.COL_ENERGY_LOST_KWH, "sum"),
        )
        .reset_index()
        .sort_values(["_year", "_month"])
        .reset_index(drop=True)
    )

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
    df = df[df[config.COL_TIME_BUCKET] != "Other"]

    agg = (
        df.groupby(config.COL_TIME_BUCKET)
        .agg(
            row_count=(config.COL_EFFICIENCY_PCT, "count"),
            avg_efficiency_pct=(config.COL_EFFICIENCY_PCT, "mean"),
            avg_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "mean"),
        )
        .reset_index()
        .rename(columns={config.COL_TIME_BUCKET: "time_bucket"})
    )

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

def _print_summary_table(summaries: list) -> None:
    """Print a one-row-per-site summary table."""
    print(f"\n{'='*158}")
    print(f"  Summary")
    print(f"{'='*158}")
    print(f"  {'Site':<22}  {'Date Range':<23}  {'Total Raw Intervals':>20}  {'Data Coverage %':>16}  {'Clean Intervals':>16}  {'Avg Efficiency':>16}  {'Avg Loss Delta':>16}  {'Loss % of Output':>18}  {'Total Energy Lost':>20}")
    print(f"  {'-'*22}  {'-'*23}  {'-'*20}  {'-'*16}  {'-'*16}  {'-'*16}  {'-'*16}  {'-'*18}  {'-'*20}")
    for s in summaries:
        date_range   = (
            f"{s['date_range_start']} – {s['date_range_end']}"
            if s["date_range_start"] else "N/A"
        )
        coverage_pct = s["clean_rows"] / s["total_raw_rows"] * 100 if s["total_raw_rows"] else float("nan")
        avg_eff      = s["avg_efficiency_pct"]
        avg_loss     = s["avg_loss_delta_kw"]
        avg_loss_pct = s["avg_loss_pct"]
        total_energy = s["total_energy_lost_kwh"]
        eff_str      = f"{avg_eff:>15.2f}%"         if pd.notna(avg_eff)      else f"{'N/A':>15} "
        loss_str     = f"{avg_loss:>14.3f} kW"      if pd.notna(avg_loss)     else f"{'N/A':>14}   "
        loss_pct_str = f"{avg_loss_pct:>15.2f}%"    if pd.notna(avg_loss_pct) else f"{'N/A':>15} "
        energy_str   = f"{total_energy:>16.1f} kWh" if pd.notna(total_energy) else f"{'N/A':>16}    "
        print(
            f"  {s['site_name']:<22}  {date_range:<23}  "
            f"{s['total_raw_rows']:>20,}  {coverage_pct:>15.1f}%  "
            f"{s['clean_rows']:>16,}  {eff_str}  {loss_str}  {loss_pct_str}  {energy_str}"
        )
    print(f"{'='*158}\n")


def run_all_reports(records: List[SiteRecord], output_dir: Path = OUTPUT_DIR) -> None:
    """Print per-site tables, a cross-site summary, and write cleaned CSVs."""
    if not records:
        print("[reporter] No records loaded — nothing to report.")
        return

    all_summaries = []

    for r in records:
        summary = summarise_site(r)
        all_summaries.append(summary)
        write_cleaned_csv(r, output_dir)

        monthly = summarise_by_month(r)
        time_df = summarise_by_time_bucket(r)
        inv_row = summarise_inverters(r)

        # ── Monthly efficiency ────────────────────────────────────────────────
        print(f"\n{'='*92}")
        print(f"  {summary['site_name']}")
        print(f"{'='*92}")
        print(f"  {'Month':<12}  {'Avg Efficiency':>16}  {'Avg Loss Delta':>16}  {'Loss % of Output':>18}  {'Total Energy Lost':>20}")
        print(f"  {'-'*12}  {'-'*16}  {'-'*16}  {'-'*18}  {'-'*20}")
        for _, row in monthly.iterrows():
            eff      = row["Average Efficiency (%)"]
            loss     = row["Average Power Loss (kW)"]
            loss_pct = row["Loss % of Output"]
            energy   = row["Total Energy Lost (kWh)"]
            eff_str      = f"{eff:>15.2f}%"          if pd.notna(eff)      else f"{'N/A':>15} "
            loss_str     = f"{loss:>14.3f} kW"        if pd.notna(loss)     else f"{'N/A':>13}   "
            loss_pct_str = f"{loss_pct:>15.2f}%"      if pd.notna(loss_pct) else f"{'N/A':>15} "
            energy_str   = f"{energy:>16.1f} kWh"     if pd.notna(energy)   else f"{'N/A':>16}    "
            print(f"  {row['Month']:<12}  {eff_str}  {loss_str}  {loss_pct_str}  {energy_str}")
        print(f"{'='*92}")
        avg_eff      = summary["avg_efficiency_pct"]
        avg_loss     = summary["avg_loss_delta_kw"]
        avg_loss_pct = summary["avg_loss_pct"]
        total_energy = summary["total_energy_lost_kwh"]
        eff_str      = f"{avg_eff:>15.2f}%"         if pd.notna(avg_eff)      else f"{'N/A':>15} "
        loss_str     = f"{avg_loss:>14.3f} kW"      if pd.notna(avg_loss)     else f"{'N/A':>13}   "
        loss_pct_str = f"{avg_loss_pct:>15.2f}%"    if pd.notna(avg_loss_pct) else f"{'N/A':>15} "
        energy_str   = f"{total_energy:>16.1f} kWh" if pd.notna(total_energy) else f"{'N/A':>16}    "
        print(f"  {'OVERALL':<12}  {eff_str}  {loss_str}  {loss_pct_str}  {energy_str}")
        print(f"{'='*92}\n")

        # ── Time of day ───────────────────────────────────────────────────────
        print(f"  Time of Day")
        print(f"  {'-'*64}")
        print(f"  {'Period':<22}  {'Readings':>10}  {'Avg Efficiency':>16}  {'Avg Loss (kW)':>13}")
        print(f"  {'-'*22}  {'-'*10}  {'-'*16}  {'-'*13}")
        for _, row in time_df.iterrows():
            print(
                f"  {row['Time Period']:<22}  "
                f"{row['Valid Readings']:>10,}  "
                f"{row['Average Efficiency (%)']:>15.2f}%  "
                f"{row['Average Power Loss (kW)']:>12.3f} kW"
            )
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

    _print_summary_table(all_summaries)
