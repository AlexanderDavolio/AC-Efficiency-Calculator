"""Data source layer — Excel workbook variant.

Accepts a single .xlsx workbook where each sheet is one site.
Supports both transitional and strict OOXML formats — strict files are patched
in memory before parsing so no manual re-save is required.

Two workbook formats are handled automatically:

Standard DAS format:
  Row 0 — column headers
  Row 1 — units row (V, A, kWh …) — skipped automatically
  Row 2+ — data rows
  Per-inverter kW derived as V × A / 1000.

ACE Built-In Query Report format:
  Rows 0–3 — preamble (title, start date, end date, blank)
  Row 4    — column headers
  Row 5    — units row — skipped automatically
  Row 6+   — data rows
  Timestamps stored as Excel serial date floats — converted to datetime.
  Per-inverter 3-phase power: P = √3 × V_LL_avg × I_avg / 1000
    where V_LL_avg = mean(VacAB, VacBC, VacCA) and I_avg = mean(IacA, IacB, IacC).
  Inverter columns discovered dynamically by regex; works for any number of inverters.
  Per-inverter phase current imbalance precomputed as a flag column consumed by cleaners.

Returns the same List[SiteRecord] contract as csv_loader.load_all_sites().
"""

import io
import re
import math
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


# ── Format detection ──────────────────────────────────────────────────────────

_ACE_HEADER_ROW = 4   # 0-indexed row that contains column names in ACE format
_ACE_UNITS_ROW  = 5   # 0-indexed row that contains units — skipped

_REQUIRED_ACE_RAW_COLUMNS = {config.COL_TIMESTAMP}


def _find_meter_col(cols, patterns: list) -> str:
    """Return the first column whose lowercased name contains any pattern (case-insensitive).

    Returns None if no match is found.
    """
    lower_map = {c.lower(): c for c in cols}
    for pat in patterns:
        pat_lc = pat.lower()
        for lc, original in lower_map.items():
            if pat_lc in lc:
                return original
    return None

_SQRT3 = math.sqrt(3)


def _is_ace_format(source) -> bool:
    """Return True if cell A1 begins with 'Ace' (ACE Built-In Query Report preamble)."""
    probe = pd.read_excel(source, sheet_name=0, header=None, nrows=1)
    return str(probe.iloc[0, 0]).strip().startswith("Ace")


# ── Standard DAS format columns ───────────────────────────────────────────────

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
    """Compute per-inverter AC power (kW) and meter average power (kW) — standard format.

    Per-inverter kW: V × A / 1000 (single-phase).
    Meter kW: per-interval kWh × (60 / INTERVAL_MINUTES).
    """
    df = df.copy()
    for col_v, col_a, col_kw in _INVERTER_POWER_SPEC:
        df[col_kw] = df[col_v] * df[col_a] / 1000.0

    kw_factor = 60.0 / config.INTERVAL_MINUTES
    df[config.COL_METER_PRODUCTION_KW] = df[config.COL_METER_KWH_RAW] * kw_factor
    return df


def _add_ace_derived_columns(df: pd.DataFrame, site_id: str) -> pd.DataFrame:
    """Normalize an ACE Built-In Query Report sheet to the standard column schema.

    Timestamps are Excel serial date floats — converted via origin='1899-12-30'.

    Meter column is discovered by scanning df.columns against the patterns in the site's
    SiteConfig (or ACE_METER_COLUMN_PATTERNS as fallback) — no hardcoded column name.

    Per-inverter 3-phase power is computed from voltage and current columns discovered by
    regex (one group per INV-N).  The voltage column suffixes and scalar factor depend on
    voltage_type from SiteConfig:
        line_to_line:    P = √3 × mean(VacAB, VacBC, VacCA) × mean(IacA, IacB, IacC) / 1000
        line_to_neutral: P =  3 × mean(VanA,  VanB,  VanC)  × mean(IacA, IacB, IacC) / 1000

    One "Inverter N AC kW" column is written per discovered inverter (real values, not a
    fake equal split).  cleaners.py detects these columns dynamically so the cross-inverter
    imbalance check uses all N inverters.

    A per-row phase current imbalance flag is precomputed and stored in
    config.COL_ACE_PHASE_IMBALANCE_FLAG for filter_phase_imbalance in cleaners.
    Standard COL_CURRENT/VOLTAGE columns are zeroed so the cross-inverter V/I checks pass.
    """
    df = df.copy()

    # Resolve site config
    site_cfg       = config.SITE_CONFIGS.get(site_id)
    meter_patterns = site_cfg.meter_patterns if site_cfg else config.ACE_METER_COLUMN_PATTERNS
    voltage_type   = site_cfg.voltage_type   if site_cfg else "line_to_line"
    expected_inv   = site_cfg.expected_inverters if site_cfg else 0

    # Convert Excel serial date floats to datetime
    df[config.COL_TIMESTAMP] = pd.to_datetime(
        pd.to_numeric(df[config.COL_TIMESTAMP], errors="coerce"),
        unit="D", origin="1899-12-30", errors="coerce",
    )

    # Discover and rename meter column
    meter_col = _find_meter_col(df.columns, meter_patterns)
    if meter_col is None:
        raise ValueError(
            f"[excel_loader] ACE site '{site_id}': no meter column matched {meter_patterns}"
        )
    df = df.rename(columns={meter_col: config.COL_METER_KWH_RAW})
    kw_factor = 60.0 / config.INTERVAL_MINUTES
    df[config.COL_METER_PRODUCTION_KW] = (
        pd.to_numeric(df[config.COL_METER_KWH_RAW], errors="coerce") * kw_factor
    )

    # Discover per-inverter column groups sorted by inverter number.
    def _find_cols(suffix: str):
        pat = re.compile(r"INV\s*-\s*(\d+).*,\s*" + re.escape(suffix) + r"$")
        matched = [(int(pat.search(c).group(1)), c) for c in df.columns if pat.search(c)]
        matched.sort(key=lambda x: x[0])
        return [c for _, c in matched]

    if voltage_type == "line_to_neutral":
        v_suffixes = ["VanA", "VanB", "VanC"]
        kw_scalar  = 3.0 / 1000.0
    else:
        v_suffixes = ["VacAB", "VacBC", "VacCA"]
        kw_scalar  = _SQRT3 / 1000.0

    v_col_groups = [_find_cols(s) for s in v_suffixes]
    iac_a = _find_cols("IacA")
    iac_b = _find_cols("IacB")
    iac_c = _find_cols("IacC")

    n_inv = len(v_col_groups[0])
    if n_inv == 0:
        raise ValueError(
            f"[excel_loader] ACE site '{site_id}': no per-inverter voltage columns found "
            f"(tried suffixes {v_suffixes})"
        )
    if expected_inv and n_inv != expected_inv:
        print(
            f"[excel_loader] WARNING: site '{site_id}' expected {expected_inv} inverters, "
            f"found {n_inv}"
        )
    print(
        f"[excel_loader] ACE site '{site_id}': {n_inv} inverters, "
        f"voltage_type={voltage_type}, meter='{meter_col}'"
    )

    imbalance_cols: list[pd.Series] = []

    for i, (col_v1, col_v2, col_v3, col_ia, col_ib, col_ic) in enumerate(
        zip(*v_col_groups, iac_a, iac_b, iac_c), start=1
    ):
        v1 = pd.to_numeric(df[col_v1], errors="coerce")
        v2 = pd.to_numeric(df[col_v2], errors="coerce")
        v3 = pd.to_numeric(df[col_v3], errors="coerce")
        ia = pd.to_numeric(df[col_ia],  errors="coerce")
        ib = pd.to_numeric(df[col_ib],  errors="coerce")
        ic = pd.to_numeric(df[col_ic],  errors="coerce")

        v_avg = (v1 + v2 + v3) / 3.0
        i_avg = (ia + ib + ic) / 3.0
        df[f"Inverter {i} AC kW"] = (v_avg * i_avg * kw_scalar).fillna(0.0)

        currents = pd.concat([ia, ib, ic], axis=1)
        mean_i   = currents.mean(axis=1)
        max_dev  = currents.sub(mean_i, axis=0).abs().max(axis=1)
        ratio    = max_dev / mean_i.where(mean_i != 0)
        imbalance_cols.append(ratio.fillna(0.0))

    # Per-row flag: True if any inverter's phase current imbalance exceeds threshold.
    max_ratio = pd.concat(imbalance_cols, axis=1).max(axis=1)
    df[config.COL_ACE_PHASE_IMBALANCE_FLAG] = (
        max_ratio > config.ACE_PHASE_CURRENT_IMBALANCE_THRESHOLD
    )

    # Zero standard V/I columns — cross-inverter imbalance checks in cleaners pass through.
    for col in [
        config.COL_VOLTAGE_A, config.COL_VOLTAGE_B, config.COL_VOLTAGE_C,
        config.COL_CURRENT_A, config.COL_CURRENT_B, config.COL_CURRENT_C,
    ]:
        df[col] = 0.0

    return df


def load_workbook(xlsx_path: Path) -> List[SiteRecord]:
    """Load every sheet from an Excel workbook and return one SiteRecord per sheet.

    Sheet name is used as site_id. Sheets missing required columns or with zero
    valid rows are skipped with a warning.
    """
    try:
        source = _to_transitional(xlsx_path)

        # Peek at cell A1 to determine which format this workbook uses.
        ace = _is_ace_format(source)
        if hasattr(source, "seek"):
            source.seek(0)

        if ace:
            print(f"[excel_loader] NOTE: '{xlsx_path.name}' is ACE Built-In Query Report format")
            all_sheets: dict = pd.read_excel(
                source, sheet_name=None, header=_ACE_HEADER_ROW, skiprows=[_ACE_UNITS_ROW]
            )
        else:
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

        if ace:
            missing = _REQUIRED_ACE_RAW_COLUMNS - set(df.columns)
            if missing:
                print(
                    f"[excel_loader] WARNING: skipping sheet '{site_id}' — "
                    f"missing columns: {sorted(missing)}"
                )
                continue

            df = _add_ace_derived_columns(df, site_id)

        else:
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

        records.append(
            SiteRecord(site_id=site_id, source_path=str(xlsx_path), raw_df=df)
        )

    return records
