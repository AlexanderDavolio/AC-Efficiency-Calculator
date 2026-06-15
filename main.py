"""Entry point — runs the full AC efficiency pipeline.

Pipeline order:
    1. LOAD    — discover input data and read all sites into SiteRecord objects
    2. CLEAN   — apply nighttime / offline / phase-imbalance / outlier filters
    3. CALCULATE — add efficiency_pct and loss_delta_kw columns
    4. REPORT  — write cleaned CSVs and the multi-tab Excel workbook

Data source routing (checked in order):
    - A .xlsx file in data/raw/ → excel_loader (each sheet = one site)
    - CSV files in data/raw/    → csv_loader   (each file = one site)
    If both exist, the .xlsx takes precedence and CSVs are ignored.
"""

from pathlib import Path
from typing import List

from src.models import SiteRecord
from src.csv_loader import load_all_sites
from src.excel_loader import load_workbook
from src.cleaners import run_all_filters
from src.calculator import run_all_calculations
from src import config


RAW_DATA_DIR = Path(__file__).resolve().parent / "data" / "raw"


def _load_records() -> List[SiteRecord]:
    """Detect what's in data/raw/ and route to the correct loader."""
    xlsx_files = sorted(RAW_DATA_DIR.glob("*.xlsx"))
    csv_files  = sorted(RAW_DATA_DIR.glob("*.csv"))

    if xlsx_files:
        if csv_files:
            print(
                f"[main] WARNING: both .xlsx and .csv files found in {RAW_DATA_DIR}; "
                f"using '{xlsx_files[0].name}', ignoring {len(csv_files)} CSV(s)"
            )
        if len(xlsx_files) > 1:
            print(
                f"[main] WARNING: {len(xlsx_files)} .xlsx files found; "
                f"using '{xlsx_files[0].name}'"
            )
        return load_workbook(xlsx_files[0])

    # No .xlsx present — fall through to the existing CSV loader.
    return load_all_sites()


def main() -> None:
    """Orchestrate the four pipeline stages end to end."""

    # ── 1. LOAD ──────────────────────────────────────────────────────────────
    records = _load_records()

    # ── 2. CLEAN + 3. CALCULATE ───────────────────────────────────────────────
    for r in records:
        r.cleaned_df  = run_all_filters(r.raw_df)
        r.enriched_df = run_all_calculations(r.cleaned_df)

    # ── 4. REPORT ────────────────────────────────────────────────────────────
    for r in records:
        avg_eff = r.enriched_df[config.COL_EFFICIENCY_PCT].mean()
        print(f"{r.site_id}: {avg_eff:.2f}%")


if __name__ == "__main__":
    main()
