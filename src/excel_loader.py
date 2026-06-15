"""Data source layer — Excel workbook variant.

Accepts a single .xlsx workbook where each sheet is one site.
Supports both transitional and strict OOXML formats — strict files are patched
in memory before parsing so no manual re-save is required.

Expected sheet structure (DAS export format):
  Row 0 — column headers
  Row 1 — units row (V, A, kWh …) — skipped automatically
  Row 2+ — data rows

Derived columns added per sheet before the SiteRecord is created:
  Inverter N AC kW  = Inverter N, AC voltage × Inverter N, AC current / 1000
  Meter kW          = meter_kWh × (60 / config.INTERVAL_MINUTES)

Returns the same List[SiteRecord] contract as csv_loader.load_all_sites().
"""

import io
import zipfile
import pandas as pd
from pathlib import Path
from typing import List, Union

from src.models import SiteRecord
from src import config


# ── Strict OOXML compatibility ────────────────────────────────────────────────

_STRICT_MARKER = b"http://purl.oclc.org/ooxml/spreadsheetml/main"

_NS_PATCHES = [
    (b"http://purl.oclc.org/ooxml/spreadsheetml/main",
     b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"),
    (b"http://purl.oclc.org/ooxml/officeDocument/relationships",
     b"http://schemas.openxmlformats.org/officeDocument/2006/relationships"),
    (b"http://purl.oclc.org/ooxml/drawingml/main",
     b"http://schemas.openxmlformats.org/drawingml/2006/main"),
    (b"http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
     b"http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"),
]


def _to_transitional(xlsx_path: Path) -> Union[Path, io.BytesIO]:
    """Return the path unchanged for normal files; return a patched BytesIO for strict OOXML."""
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        if _STRICT_MARKER not in zf.read("xl/workbook.xml"):
            return xlsx_path

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zf.infolist():
                data = zf.read(item.filename)
                if item.filename.endswith(".xml") or item.filename.endswith(".rels"):
                    for strict_ns, trans_ns in _NS_PATCHES:
                        data = data.replace(strict_ns, trans_ns)
                    data = data.replace(b' conformance="strict"', b"")
                zout.writestr(item, data)
        buf.seek(0)

    print(f"[excel_loader] NOTE: '{xlsx_path.name}' uses strict OOXML — converted to transitional in memory")
    return buf


# ── Column requirements ───────────────────────────────────────────────────────

# Raw columns that must be present before derived kW columns can be computed.
_REQUIRED_RAW_COLUMNS = {
    config.COL_TIMESTAMP,
    config.COL_METER_KWH_RAW,
    config.COL_VOLTAGE_A, config.COL_CURRENT_A,
    config.COL_VOLTAGE_B, config.COL_CURRENT_B,
    config.COL_VOLTAGE_C, config.COL_CURRENT_C,
}

# (voltage col, current col, derived kW col) — one entry per inverter
_INVERTER_POWER_SPEC = [
    (config.COL_VOLTAGE_A, config.COL_CURRENT_A, config.COL_INV1_AC_KW),
    (config.COL_VOLTAGE_B, config.COL_CURRENT_B, config.COL_INV2_AC_KW),
    (config.COL_VOLTAGE_C, config.COL_CURRENT_C, config.COL_INV3_AC_KW),
]


def _add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-inverter AC power (kW) and meter average power (kW).

    Per-inverter kW: V × A / 1000 (single-phase).
    Meter kW: per-interval kWh × (60 / INTERVAL_MINUTES).
    """
    df = df.copy()
    for col_v, col_a, col_kw in _INVERTER_POWER_SPEC:
        df[col_kw] = df[col_v] * df[col_a] / 1000.0

    kw_factor = 60.0 / config.INTERVAL_MINUTES
    df[config.COL_METER_PRODUCTION_KW] = df[config.COL_METER_KWH_RAW] * kw_factor
    return df


def load_workbook(xlsx_path: Path) -> List[SiteRecord]:
    """Load every sheet from an Excel workbook and return one SiteRecord per sheet.

    Sheet name is used as site_id. Sheets missing required columns or with zero
    valid rows are skipped with a warning.
    """
    try:
        source = _to_transitional(xlsx_path)
        # skiprows=[1] drops the units row (row index 1 after the header row).
        all_sheets: dict = pd.read_excel(source, sheet_name=None, header=0, skiprows=[1])
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

        # Validate required raw columns before doing any further work on this sheet.
        missing = _REQUIRED_RAW_COLUMNS - set(df.columns)
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

        # Compute derived power columns from voltage × current and kWh → kW.
        df = _add_derived_columns(df)

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
        derived_numeric = numeric_cols + [config.COL_METER_PRODUCTION_KW] + [c for _, _, c in _INVERTER_POWER_SPEC]
        null_counts = df[[config.COL_METER_PRODUCTION_KW, *[c for _, _, c in _INVERTER_POWER_SPEC]]].isna().sum()
        null_summary = ", ".join(
            f"{col}={n}" for col, n in null_counts.items() if n > 0
        ) or "none"

        print(
            f"\n[{site_id}] loaded"
            f"\n  rows      : {len(df):,}"
            f"\n  date range: {date_range}"
            f"\n  columns   : {list(df.columns)}"
            f"\n  nulls (derived kW cols): {null_summary}\n"
        )

        records.append(
            SiteRecord(site_id=site_id, source_path=str(xlsx_path), raw_df=df)
        )

    return records
