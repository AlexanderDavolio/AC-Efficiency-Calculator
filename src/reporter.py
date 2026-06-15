"""Output layer: per-site cleaned CSVs and a single multi-tab Excel report."""

import calendar
import os
import re
from pathlib import Path
from typing import List

import pandas as pd

from src.models import SiteRecord
from src import config


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _autofit_columns(worksheet) -> None:
    """Set each column width to the longest value in that column plus padding."""
    for col in worksheet.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        worksheet.column_dimensions[col[0].column_letter].width = max_len + 2


def _write_sheet(writer: pd.ExcelWriter, df: pd.DataFrame, sheet_name: str) -> None:
    """Write df to a named sheet, auto-fit column widths, and freeze the header row."""
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    _autofit_columns(ws)
    ws.freeze_panes = "A2"


# ── Per-site summaries ────────────────────────────────────────────────────────

def summarise_site(record: SiteRecord) -> dict:
    """Return a summary dict of key metrics for one site using its enriched_df."""
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
        "avg_loss_delta_kw":  round(df[config.COL_LOSS_DELTA_KW].mean(), 3),
        "date_range_start":   ts.min().date() if not ts.empty else None,
        "date_range_end":     ts.max().date() if not ts.empty else None,
    }

    record.summary = summary
    return summary


def summarise_by_month(record: SiteRecord) -> pd.DataFrame:
    """Return a DataFrame with one row per (year, month) with display-ready column names."""
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
            total_loss_delta_kw=(config.COL_LOSS_DELTA_KW, "sum"),
        )
        .reset_index()
        .sort_values(["_year", "_month"])
        .reset_index(drop=True)
    )

    float_cols = ["avg_efficiency_pct", "min_efficiency_pct", "max_efficiency_pct",
                  "avg_loss_delta_kw", "total_loss_delta_kw"]
    agg[float_cols] = agg[float_cols].round(3)

    month_labels = agg.apply(
        lambda r: f"{calendar.month_abbr[int(r['_month'])]} {int(r['_year'])}", axis=1
    )

    return pd.DataFrame({
        "Site":                    record.site_id,
        "Month":                   month_labels,
        "Valid Readings":          agg["row_count"],
        "Average Efficiency (%)":  agg["avg_efficiency_pct"],
        "Min Efficiency (%)":      agg["min_efficiency_pct"],
        "Max Efficiency (%)":      agg["max_efficiency_pct"],
        "Average Power Loss (kW)": agg["avg_loss_delta_kw"],
        "Total Power Loss (kW)":   agg["total_loss_delta_kw"],
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

    # Sort into natural time-of-day order before remapping labels.
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
        "Site":                    record.site_id,
        "Time Period":             agg["time_bucket"],
        "Valid Readings":          agg["row_count"],
        "Average Efficiency (%)":  agg["avg_efficiency_pct"],
        "Average Power Loss (kW)": agg["avg_loss_delta_kw"],
    })


def summarise_inverters(record: SiteRecord) -> dict:
    """Return a dict of inverter power share metrics with a scaled imbalance note.

    Equal share = 100 / n_inverters. A site is flagged if any inverter deviates
    more than 10 percentage points from that equal share.
    """
    df = record.enriched_df
    valid = df[df[config.COL_TOTAL_INVERTER_KW] > 0]

    inv_cols = [c for c in df.columns if re.match(r"^Inverter \d+ AC kW$", c)]
    if not inv_cols:
        inv_cols = config.INVERTER_KW_COLS
    inv_cols = sorted(inv_cols, key=lambda c: int(re.search(r"\d+", c).group()))

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


# ── Outputs ───────────────────────────────────────────────────────────────────

def write_cleaned_csv(record: SiteRecord, output_dir: Path = OUTPUT_DIR) -> Path:
    """Write enriched_df to output/<site_id>_cleaned.csv and return the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record.site_id}_cleaned.csv"
    record.enriched_df.to_csv(path, index=False)
    print(f"[reporter] wrote cleaned CSV   : {path}")
    return path


# ── Excel output ─────────────────────────────────────────────────────────────

def write_excel_report(records: List[SiteRecord], output_dir: Path = OUTPUT_DIR) -> Path:
    """Write a single Excel file with Monthly Breakdown, Time of Day, and Inverter Split tabs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "efficiency_report.xlsx"

    monthly_df     = pd.concat([summarise_by_month(r)       for r in records], ignore_index=True)
    time_of_day_df = pd.concat([summarise_by_time_bucket(r) for r in records], ignore_index=True)
    inverter_df    = pd.DataFrame([summarise_inverters(r)   for r in records])

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _write_sheet(writer, monthly_df,     "Monthly Breakdown")
        _write_sheet(writer, time_of_day_df, "Time of Day")
        _write_sheet(writer, inverter_df,    "Inverter Split")

    print(f"[reporter] wrote Excel report  : {path}")
    os.startfile(path)
    return path


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_all_reports(records: List[SiteRecord], output_dir: Path = OUTPUT_DIR) -> None:
    """Write per-site cleaned CSVs and the multi-tab Excel report."""
    for r in records:
        summarise_site(r)
        write_cleaned_csv(r, output_dir)

    write_excel_report(records, output_dir)
