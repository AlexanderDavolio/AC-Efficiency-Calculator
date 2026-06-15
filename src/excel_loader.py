"""Data source layer — Excel workbook variant.

Accepts a single .xlsx workbook where each sheet is one site.
Returns the same List[SiteRecord] contract as csv_loader.load_all_sites().
"""

import pandas as pd
from pathlib import Path
from typing import List

from src.models import SiteRecord
from src import config


# Every sheet must contain these columns to be processed.
_REQUIRED_COLUMNS = {
    config.COL_TIMESTAMP,
    config.COL_METER_PRODUCTION_KW,
    config.COL_INV1_AC_KW,
    config.COL_INV2_AC_KW,
    config.COL_CURRENT_A,
    config.COL_CURRENT_B,
    config.COL_CURRENT_C,
    config.COL_VOLTAGE_A,
    config.COL_VOLTAGE_B,
    config.COL_VOLTAGE_C,
}


def load_workbook(xlsx_path: Path) -> List[SiteRecord]:
    """Load every sheet from an Excel workbook and return one SiteRecord per sheet.

    Sheet name is used as site_id. Sheets missing required columns or with zero
    valid rows are skipped with a warning.
    """
    try:
        all_sheets: dict = pd.read_excel(xlsx_path, sheet_name=None, header=0)
    except Exception as exc:
        raise RuntimeError(
            f"[excel_loader] Cannot open workbook '{xlsx_path}': {exc}"
        ) from exc

    if not all_sheets:
        print(f"[excel_loader] WARNING: workbook '{xlsx_path.name}' contains no sheets")
        return []

    records = []

    for sheet_name, df in all_sheets.items():
        site_id = sheet_name

        # Strip column name whitespace so comparisons against config constants work.
        df.columns = df.columns.str.strip()

        # Validate required columns before doing any further work on this sheet.
        missing = _REQUIRED_COLUMNS - set(df.columns)
        if missing:
            print(
                f"[excel_loader] WARNING: skipping sheet '{site_id}' — "
                f"missing columns: {sorted(missing)}"
            )
            continue

        # Parse timestamp column; invalid entries become NaT.
        df[config.COL_TIMESTAMP] = pd.to_datetime(
            df[config.COL_TIMESTAMP], errors="coerce"
        )

        # Coerce all non-timestamp columns to float; non-numeric values become NaN.
        numeric_cols = [c for c in df.columns if c != config.COL_TIMESTAMP]
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")

        if len(df) == 0:
            print(f"[excel_loader] WARNING: skipping sheet '{site_id}' — zero rows")
            continue

        # ── Sanity check ────────────────────────────────────────────────────────
        ts = df[config.COL_TIMESTAMP].dropna()
        date_range = (
            f"{ts.min().date()} to {ts.max().date()}"
            if not ts.empty
            else "no valid timestamps"
        )
        null_counts = df[numeric_cols].isna().sum()
        null_summary = ", ".join(
            f"{col}={n}" for col, n in null_counts.items() if n > 0
        ) or "none"

        print(
            f"\n[{site_id}] loaded"
            f"\n  rows      : {len(df):,}"
            f"\n  date range: {date_range}"
            f"\n  columns   : {list(df.columns)}"
            f"\n  nulls     : {null_summary}\n"
        )

        records.append(
            SiteRecord(site_id=site_id, source_path=str(xlsx_path), raw_df=df)
        )

    return records
