"""Data source layer — CSV variant.

Handles AlsoEnergy (ACE) Built-In Query Report CSV exports.

ACE CSV format:
  Row 0 — report title ("Site Name - Built-In Query Report")
  Row 1 — start date
  Row 2 — end date
  Row 3 — blank
  Row 4 — column headers
  Row 5 — units row (skipped)
  Row 6+ — data rows
"""

import re

import pandas as pd
from pathlib import Path
from typing import List

from src.models import SiteRecord
from src import config


RAW_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

_ACE_HEADER_ROW = 4
_ACE_UNITS_ROW  = 5


def _find_col(cols, patterns: list) -> str:
    """Return first column whose lowercased name contains any pattern; None if no match."""
    lower_map = {c.lower(): c for c in cols}
    for pat in patterns:
        pat_lc = pat.lower()
        for lc, original in lower_map.items():
            if pat_lc in lc:
                return original
    return None


def discover_sites(raw_dir: Path = RAW_DATA_DIR) -> List[Path]:
    """Return sorted list of CSV paths in raw_dir."""
    return sorted(raw_dir.glob("*.csv"))


def load_site(csv_path: Path) -> SiteRecord:
    """Read a single ACE CSV export and return a populated SiteRecord.

    site_id is taken from preamble row 0 cell 0, with the " - Built-In Query Report"
    suffix stripped (same logic as excel_loader).
    """
    # Extract site_id from preamble row 0
    preamble = pd.read_csv(csv_path, header=None, nrows=1)
    raw_name = str(preamble.iloc[0, 0]).strip()
    site_id = raw_name.partition(" - ")[0].strip()

    if site_id not in config.SITE_CONFIGS:
        print(
            f"[csv_loader] WARNING: site '{site_id}' not found in SITE_CONFIGS — "
            f"add an entry to config.py to configure meter/inverter patterns"
        )

    # Read data with ACE layout: headers on row 4, skip units row 5
    df = pd.read_csv(csv_path, header=_ACE_HEADER_ROW, skiprows=[_ACE_UNITS_ROW])
    df.columns = df.columns.str.strip()

    site_cfg = config.SITE_CONFIGS.get(site_id)
    meter_patterns = (
        site_cfg.meter_patterns
        if site_cfg and site_cfg.meter_patterns is not None
        else config.ACE_METER_COLUMN_PATTERNS
    )
    inv_patterns = (
        site_cfg.inverter_patterns
        if site_cfg and site_cfg.inverter_patterns is not None
        else config.ACE_INVERTER_COLUMN_PATTERNS
    )

    kw_factor = 60.0 / config.INTERVAL_MINUTES

    # Timestamps — CSV strings are directly parseable
    df[config.COL_TIMESTAMP] = pd.to_datetime(df[config.COL_TIMESTAMP], errors="coerce")

    # Meter kW
    meter_col = _find_col(df.columns, meter_patterns)
    if meter_col is None:
        raise ValueError(
            f"[csv_loader] site '{site_id}': no meter column matched {meter_patterns}"
        )
    df[config.COL_METER_PRODUCTION_KW] = (
        pd.to_numeric(df[meter_col], errors="coerce") * kw_factor
    )

    # Per-inverter kW — discover columns via config patterns, extract number from name
    seen_nums: dict = {}
    for c in df.columns:
        c_lc = c.lower()
        for pat in inv_patterns:
            if pat.lower() in c_lc:
                m = re.search(r"\d+", c)
                if m:
                    num = int(m.group())
                    if num not in seen_nums:
                        seen_nums[num] = c
                break
    inv_kwh_matches = sorted(seen_nums.items(), key=lambda x: x[0])

    if not inv_kwh_matches:
        raise ValueError(
            f"[csv_loader] site '{site_id}': no inverter kWh columns found "
            f"(patterns searched: {inv_patterns})"
        )

    for inv_num, kwh_col in inv_kwh_matches:
        df[kwh_col] = pd.to_numeric(df[kwh_col], errors="coerce")
        df[f"Inverter {inv_num} AC kW"] = (df[kwh_col] * kw_factor).fillna(0.0)

    # Coerce V/I columns to numeric; pass through as raw
    for pat in config.ACE_METER_VOLTAGE_PATTERNS + config.ACE_METER_CURRENT_PATTERNS:
        col = _find_col(df.columns, [pat])
        if col is not None:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    n_inv = len(inv_kwh_matches)
    print(f"[csv_loader] ACE site '{site_id}': {n_inv} inverters, meter='{meter_col}'")

    # Sort by timestamp
    df = df.sort_values(config.COL_TIMESTAMP).reset_index(drop=True)

    # Sanity check
    ts = df[config.COL_TIMESTAMP].dropna()
    date_range = (
        f"{ts.min().date()} to {ts.max().date()}"
        if not ts.empty
        else "no valid timestamps"
    )
    inv_kw_cols = sorted(
        [c for c in df.columns if re.match(r"^Inverter \d+ AC kW$", c)],
        key=lambda c: int(re.search(r"\d+", c).group()),
    )
    null_counts = df[[config.COL_METER_PRODUCTION_KW, *inv_kw_cols]].isna().sum()
    null_summary = (
        ", ".join(f"{col}={n}" for col, n in null_counts.items() if n > 0) or "none"
    )

    print(
        f"\n[{site_id}] loaded"
        f"\n  rows      : {len(df):,}"
        f"\n  date range: {date_range}"
        f"\n  columns   : {list(df.columns)}"
        f"\n  nulls (derived kW cols): {null_summary}\n"
    )

    return SiteRecord(site_id=site_id, source_path=str(csv_path), raw_df=df)


def load_all_sites(raw_dir: Path = RAW_DATA_DIR) -> List[SiteRecord]:
    """Discover and load every ACE CSV in raw_dir, returning one SiteRecord each."""
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
