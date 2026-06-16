"""Output layer: per-site cleaned CSVs and console tables (monthly, daily quality, time of day, inverter split)."""

import calendar
import contextlib
import io
import re
from pathlib import Path
from typing import List

import pandas as pd

from src.models import SiteRecord
from src import config


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _good_day_df(record) -> pd.DataFrame:
    """Return enriched_df rows restricted to good days; falls back to all rows if daily_df is unset."""
    if record.daily_df is None or record.daily_df.empty:
        return record.enriched_df
    good_dates = set(record.daily_df.loc[record.daily_df["is_good_day"], "date"])
    row_dates  = pd.to_datetime(record.enriched_df[config.COL_TIMESTAMP]).dt.date
    return record.enriched_df[row_dates.isin(good_dates)]


# ── Per-site summaries ────────────────────────────────────────────────────────

def summarise_site(record: SiteRecord) -> dict:
    """Return a summary dict. Row counts use all clean data; efficiency metrics are good-day-only."""
    df  = record.enriched_df
    gdf = _good_day_df(record)
    ts  = df[config.COL_TIMESTAMP]

    summary = {
        "site_name":          record.site_id,
        "total_raw_rows":     len(record.raw_df),
        "clean_rows":         len(df),
        "clean_row_pct":      round(len(df) / len(record.raw_df) * 100, 2),
        "avg_efficiency_pct": round(gdf[config.COL_EFFICIENCY_PCT].mean(), 3),
        "min_efficiency_pct": round(gdf[config.COL_EFFICIENCY_PCT].min(), 3),
        "max_efficiency_pct": round(gdf[config.COL_EFFICIENCY_PCT].max(), 3),
        "avg_loss_delta_kw":  round(gdf[config.COL_LOSS_DELTA_KW].mean(), 3),
        "date_range_start":   ts.min().date() if not ts.empty else None,
        "date_range_end":     ts.max().date() if not ts.empty else None,
    }

    record.summary = summary
    return summary


def summarise_by_month(record: SiteRecord) -> pd.DataFrame:
    """Return a DataFrame with one row per (year, month), computed over good-day rows only."""
    df = _good_day_df(record).copy()
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


# ── Sensitivity analysis ──────────────────────────────────────────────────────

def print_sensitivity_analysis(record: SiteRecord) -> None:
    """Re-run the pipeline at 2% and 3% phase imbalance thresholds and print a comparison table."""
    from src.cleaners import run_all_filters
    from src.calculator import run_all_calculations

    thresholds = [0.01, 0.02, 0.03]
    rows_total = len(record.raw_df)
    results = []

    for t in thresholds:
        with contextlib.redirect_stdout(io.StringIO()):
            cleaned  = run_all_filters(record.raw_df, record.site_id, phase_threshold=t)
            enriched = run_all_calculations(cleaned)

        results.append({
            "label":    f"{int(t * 100)}% (official threshold)" if t == 0.01 else f"{int(t * 100)}%",
            "kept":     len(cleaned),
            "dropped":  rows_total - len(cleaned),
            "avg_eff":  enriched[config.COL_EFFICIENCY_PCT].mean(),
            "avg_loss": enriched[config.COL_LOSS_DELTA_KW].mean(),
        })

    print(f"\n  Phase Imbalance Sensitivity Analysis")
    print(f"  {'-'*73}")
    print(f"  {'Threshold':<28}  {'Rows Kept':>10}  {'Rows Dropped':>12}  {'Avg Efficiency':>16}  {'Avg Loss Delta':>14}")
    print(f"  {'-'*28}  {'-'*10}  {'-'*12}  {'-'*16}  {'-'*14}")
    for r in results:
        print(
            f"  {r['label']:<28}  "
            f"{r['kept']:>10,}  "
            f"{r['dropped']:>12,}  "
            f"{r['avg_eff']:>15.2f}%  "
            f"{r['avg_loss']:>13.3f} kW"
        )
    print(f"  {'-'*73}\n")


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_all_reports(records: List[SiteRecord], output_dir: Path = OUTPUT_DIR) -> None:
    """Print per-site summary tables and write cleaned CSVs."""
    if not records:
        print("[reporter] No records loaded — nothing to report.")
        return

    for r in records:
        summary = summarise_site(r)
        write_cleaned_csv(r, output_dir)

        monthly = summarise_by_month(r)
        time_df = summarise_by_time_bucket(r)
        inv_row = summarise_inverters(r)

        dd = r.daily_df
        if dd is not None and not dd.empty:
            n_good  = int(dd["is_good_day"].sum())
            n_total = len(dd)
            day_label = f"  [{n_good}/{n_total} good days]"
        else:
            day_label = ""

        # ── Monthly efficiency ────────────────────────────────────────────────
        print(f"\n{'='*52}")
        print(f"  {summary['site_name']}{day_label}")
        print(f"  (figures are good-day-only averages)")
        print(f"{'='*52}")
        print(f"  {'Month':<12}  {'Avg Efficiency':>16}  {'Avg Loss Delta':>16}")
        print(f"  {'-'*12}  {'-'*16}  {'-'*16}")
        for _, row in monthly.iterrows():
            eff  = row["Average Efficiency (%)"]
            loss = row["Average Power Loss (kW)"]
            eff_str  = f"{eff:>15.2f}%"    if pd.notna(eff)  else f"{'N/A':>15} "
            loss_str = f"{loss:>14.3f} kW" if pd.notna(loss) else f"{'N/A':>13}   "
            print(f"  {row['Month']:<12}  {eff_str}  {loss_str}")
        print(f"{'='*52}")
        avg_eff  = summary["avg_efficiency_pct"]
        avg_loss = summary["avg_loss_delta_kw"]
        eff_str  = f"{avg_eff:>15.2f}%"    if pd.notna(avg_eff)  else f"{'N/A':>15} "
        loss_str = f"{avg_loss:>14.3f} kW" if pd.notna(avg_loss) else f"{'N/A':>13}   "
        print(f"  {'OVERALL':<12}  {eff_str}  {loss_str}")
        print(f"{'='*52}\n")

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

        # ── Daily quality by month ────────────────────────────────────────────
        if dd is not None and not dd.empty:
            dd2 = dd.copy()
            dd2["_ym"]    = dd2["date"].apply(lambda d: d.year * 100 + d.month)
            dd2["_label"] = dd2["date"].apply(
                lambda d: f"{calendar.month_abbr[d.month]} {d.year}"
            )
            dq = (
                dd2.groupby(["_ym", "_label"])
                .agg(total=("is_good_day", "count"), good=("is_good_day", "sum"),
                     avg_cov=("pct_clean", "mean"))
                .reset_index()
                .sort_values("_ym")
                .reset_index(drop=True)
            )
            dq["bad"]      = dq["total"] - dq["good"]
            dq["good_pct"] = (dq["good"] / dq["total"] * 100).round(1)
            dq["avg_cov"]  = (dq["avg_cov"] * 100).round(1)

            print(f"  Daily Quality by Month")
            print(f"  {'-'*62}")
            print(f"  {'Month':<12}  {'Days':>5}  {'Good':>5}  {'Bad':>5}  {'Good %':>7}  {'Avg Coverage':>13}")
            print(f"  {'-'*12}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*7}  {'-'*13}")
            for _, row in dq.iterrows():
                print(
                    f"  {row['_label']:<12}  "
                    f"{int(row['total']):>5}  "
                    f"{int(row['good']):>5}  "
                    f"{int(row['bad']):>5}  "
                    f"{row['good_pct']:>6.1f}%  "
                    f"{row['avg_cov']:>12.1f}%"
                )
            print()

        print_sensitivity_analysis(r)
