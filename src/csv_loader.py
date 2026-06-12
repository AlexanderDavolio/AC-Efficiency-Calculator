"""Data source layer.

This module is the only place that knows where data comes from.
To swap CSVs for a database or API: implement a new loader function here
(e.g. load_from_api) with the same return signature, then update main.py
to call it instead — no other file needs to change.
"""

import pandas as pd
from pathlib import Path
from typing import List

from src.models import SiteRecord
from src import config


# Default location for raw CSV files.
RAW_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def discover_sites(raw_dir: Path = RAW_DATA_DIR) -> List[Path]:
    """Return a sorted list of CSV paths found in raw_dir."""
    return sorted(raw_dir.glob("*.csv"))


def load_site(csv_path: Path) -> SiteRecord:
    """Read a single CSV file and return a populated SiteRecord.

    Column names are stripped of leading/trailing whitespace.
    The timestamp column is parsed to datetime.
    A sanity-check summary is printed to the console after loading.
    """
    site_id = csv_path.stem

    df = pd.read_csv(csv_path)

    # Normalise column names so "  Site Time  " and "Site Time" are the same.
    df.columns = df.columns.str.strip()

    # Parse timestamps in place; invalid values become NaT rather than raising.
    df[config.COL_TIMESTAMP] = pd.to_datetime(df[config.COL_TIMESTAMP], errors="coerce")

    # Coerce every non-timestamp column to float. Non-numeric strings become NaN
    # instead of propagating as object dtype and crashing arithmetic downstream.
    numeric_cols = [c for c in df.columns if c != config.COL_TIMESTAMP]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")

    # ── Sanity check ────────────────────────────────────────────────────────
    ts = df[config.COL_TIMESTAMP].dropna()
    date_range = (
        f"{ts.min().date()} → {ts.max().date()}" if not ts.empty else "no valid timestamps"
    )
    print(
        f"\n[{site_id}] loaded"
        f"\n  rows      : {len(df):,}"
        f"\n  date range: {date_range}"
        f"\n  columns   : {list(df.columns)}\n"
    )

    return SiteRecord(site_id=site_id, source_path=str(csv_path), raw_df=df)


def load_all_sites(raw_dir: Path = RAW_DATA_DIR) -> List[SiteRecord]:
    """Discover and load every site CSV in raw_dir, returning one SiteRecord each.

    Files that cannot be parsed are skipped with a warning.
    """
    paths = discover_sites(raw_dir)
    if not paths:
        print(f"[csv_loader] No CSV files found in {raw_dir}")
        return []

    records = []
    for path in paths:
        try:
            records.append(load_site(path))
        except Exception as exc:
            print(f"[csv_loader] WARNING: skipping {path.name} — {exc}")

    return records
