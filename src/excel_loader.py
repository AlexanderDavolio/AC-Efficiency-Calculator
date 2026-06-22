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
  Per-inverter kW: inverter kWh × (60 / INTERVAL_MINUTES); columns discovered via site config inverter_patterns.
  Meter kW: Wattnode Meter kWh ÷ interval_hours.
  Meter phase voltages (VacA/B/C) and currents (IacA/B/C) passed through as raw numeric columns.

Returns the same List[SiteRecord] contract as csv_loader.load_all_sites().
"""

import io
import re
import zipfile
import pandas as pd
from pathlib import Path
from typing import List, Union

from src.models import SiteRecord
from src import config
from src.cumulative import to_interval_if_cumulative


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


# Per-phase voltage / current / power-factor sub-columns share the meter's base name
# (e.g. "Generation Meter (PS1), IacA"), so a substring meter match would wrongly catch
# them. These tokens identify and exclude such auxiliary channels.
_METER_AUX_PATTERNS = (
    config.ACE_METER_VOLTAGE_PATTERNS
    + config.ACE_METER_CURRENT_PATTERNS
    + ["Power factor"]
)


def _find_meter_cols(cols, patterns: list) -> list:
    """Return ALL energy columns matching any meter pattern, in column order.

    Unlike _find_meter_col (first match only), this is used for configured sites whose
    production meter is split across several columns that must be summed — e.g. one
    generation meter per power station. Per-phase V/I and power-factor sub-columns are
    excluded so only the base meter-energy columns are returned.
    """
    out = []
    for c in cols:
        lc = c.lower()
        if any(aux.lower() in lc for aux in _METER_AUX_PATTERNS):
            continue
        if any(pat.lower() in lc for pat in patterns):
            out.append(c)
    return out


def _auto_detect_meter_col(cols: list) -> str:
    """Broad meter detection for sites not in SITE_CONFIGS.

    Tries priority keywords first; falls back to any kWh column without a digit
    (likely an aggregate total rather than a per-inverter channel).
    """
    lower_map = {c.lower(): c for c in cols}
    for kw in ("production", "net energy", "meter", "net kwh"):
        for lc, orig in lower_map.items():
            if kw in lc:
                return orig
    # Generic kWh fallback — prefer columns without digits (not per-inverter)
    for lc, orig in lower_map.items():
        if "kwh" in lc and not re.search(r"\d", lc):
            return orig
    for lc, orig in lower_map.items():
        if "kwh" in lc:
            return orig
    return None


_NON_INVERTER_SUBSTRINGS = {
    "timestamp", "inverters",  # "inverters" plural = aggregate; "inverter" singular = individual channel
    "site performance estimate",
    "power factor", "average ac voltage", "total ac current",
    "vaca", "vacb", "vacc", "vacab", "vacbc", "vacca",
    "iaca", "iacb", "iacc",
}


def _is_non_inverter_col(col: str) -> bool:
    """Return True if col should never be treated as a per-inverter energy channel."""
    lc = col.lower()
    return any(excl in lc for excl in _NON_INVERTER_SUBSTRINGS)


# A column is "inverter-named" if it contains "inverter" OR the standalone token
# "inv" (e.g. "Chint CPS-SCA60KTL INV - 1"). The \binv\b word boundary deliberately
# does NOT match "inverters" (the aggregate) — that stays excluded via _NON_INVERTER_SUBSTRINGS.
_INV_NAME_RE = re.compile(r"inverter|\binv\b", re.IGNORECASE)


def _auto_detect_inverter_cols(df: pd.DataFrame, exclude: str) -> list:
    """Return columns that look like per-interval inverter kWh readings.

    Primary path: columns whose name contains 'inverter' OR the standalone token
    'inv' (matches every known naming convention: "INVERTER 1", "Sungrow 60KW
    Inverter - A1", "Chint CPS-SCA60KTL INV - 1", etc.) after removing the meter
    columns and known non-inverter substrings.

    Fallback: if no inverter-named columns survive, broaden to all remaining numeric
    columns and log a warning — handles sites with atypical naming.

    The candidate pool always excludes the raw meter column, the derived "Meter kW"
    column, and the "Inverters" aggregate (via _NON_INVERTER_SUBSTRINGS), so no
    aggregate or meter column is ever summed as an individual channel under any path.

    Remaining candidates are numbered 1, 2, 3… in column order.
    """
    pre_filter = [
        c for c in df.columns
        if c != exclude
        and c != config.COL_METER_PRODUCTION_KW  # derived meter kW added before detection — never an inverter
        and not _is_non_inverter_col(c)
    ]

    # Prefer explicitly inverter-named columns ("inverter" or standalone "inv").
    inv_named = [c for c in pre_filter if _INV_NAME_RE.search(c)]
    candidates = inv_named if inv_named else pre_filter

    if not inv_named and pre_filter:
        print(
            f"[excel_loader] WARNING: no inverter-named columns found — "
            f"falling back to broad numeric scan ({len(pre_filter)} candidate(s))"
        )

    valid = []
    for c in candidates:
        numeric = pd.to_numeric(df[c], errors="coerce")
        if numeric.isna().all():
            continue
        nonzero = numeric[numeric != 0]
        # Exclude columns whose non-zero values are all negligibly small — catches
        # status-flag columns that happen to contain occasional 0/1 integers.
        if nonzero.empty or nonzero.median() == 0:
            continue
        valid.append(c)
    return [(i + 1, [c]) for i, c in enumerate(valid)]


_INTERVAL_TOLERANCE_MIN = 2


def _validate_intervals(df: pd.DataFrame, site_id: str) -> None:
    """Log a warning if any consecutive timestamp gap falls outside the expected interval.

    Diagnostic only — does not modify the DataFrame or raise.
    """
    ts = df[config.COL_TIMESTAMP].dropna().sort_values()
    if len(ts) < 2:
        return

    delta_min = ts.diff().dropna().dt.total_seconds() / 60
    lo = config.INTERVAL_MINUTES - _INTERVAL_TOLERANCE_MIN
    hi = config.INTERVAL_MINUTES + _INTERVAL_TOLERANCE_MIN
    bad = delta_min[(delta_min < lo) | (delta_min > hi)]

    if not bad.empty:
        print(
            f"[excel_loader] WARNING: site '{site_id}' — {len(bad):,} intervals outside "
            f"{lo}–{hi} min (expected {config.INTERVAL_MINUTES} min); "
            f"min={bad.min():.1f} min, max={bad.max():.1f} min"
        )


def _add_ace_derived_columns(df: pd.DataFrame, site_id: str) -> pd.DataFrame:
    """Normalize an ACE Built-In Query Report sheet to the standard column schema.

    Timestamps are Excel serial date floats — converted via origin='1899-12-30'.

    Meter kWh column discovered by keyword scan (_auto_detect_meter_col); converted
    to average power as kWh × (60 / INTERVAL_MINUTES).

    Per-inverter kWh columns discovered by _auto_detect_inverter_cols (columns whose
    name contains 'inverter', after excluding known non-inverter substrings). Each
    column is converted to 'Inverter N AC kW'. Multiple columns sharing the same
    inverter number are summed before conversion. cleaners.py and calculator.py
    discover these derived columns via the regex ^Inverter \\d+ AC kW$.

    Meter phase voltage (VacA/B/C) and current (IacA/B/C) columns are coerced to
    numeric and passed through as raw columns for downstream phase-imbalance checks.
    """
    df = df.copy()

    site_cfg = config.SITE_CONFIGS.get(site_id)

    # Convert timestamps — openpyxl may already parse date cells as datetime objects;
    # fall back to Excel serial date float conversion if direct parsing yields all NaT.
    ts_raw = df[config.COL_TIMESTAMP]
    ts = pd.to_datetime(ts_raw, errors="coerce")
    if ts.isna().all():
        ts = pd.to_datetime(
            pd.to_numeric(ts_raw, errors="coerce"),
            unit="D", origin="1899-12-30", errors="coerce",
        )
    df[config.COL_TIMESTAMP] = ts

    kw_factor = 60.0 / config.INTERVAL_MINUTES  # kWh → kW for INTERVAL_MINUTES-min readings

    # ── Meter column detection ────────────────────────────────────────────────
    # meter_cols may hold more than one column for multi-meter sites (e.g. one
    # generation meter per power station); they are summed into the site meter.
    # Auto-detect unless the site config explicitly supplies meter patterns — so a config
    # that carries only reporting metadata (e.g. excluded_months) doesn't change ingestion.
    if site_cfg is None or site_cfg.meter_patterns is None:
        single = _auto_detect_meter_col(list(df.columns))
        if not single:
            raise ValueError(
                f"[excel_loader] ACE site '{site_id}': could not auto-detect meter column"
            )
        meter_cols = [single]
        print(f"[excel_loader] auto-detect '{site_id}': using '{single}' as meter column")
    else:
        meter_patterns = site_cfg.meter_patterns
        meter_cols = _find_meter_cols(df.columns, meter_patterns)
        if not meter_cols:
            raise ValueError(
                f"[excel_loader] ACE site '{site_id}': no meter column matched {meter_patterns}"
            )
        if len(meter_cols) > 1:
            print(f"[excel_loader] site '{site_id}': meter = sum of {len(meter_cols)} columns {meter_cols}")
        else:
            print(f"[excel_loader] site '{site_id}': using '{meter_cols[0]}' as meter column")

    # Convert any cumulative register per source column, then sum, then kWh -> kW.
    df[config.COL_METER_PRODUCTION_KW] = (
        pd.concat(
            [
                to_interval_if_cumulative(
                    pd.to_numeric(df[c], errors="coerce"),
                    df[config.COL_TIMESTAMP], c, site_id,
                )
                for c in meter_cols
            ],
            axis=1,
        ).sum(axis=1, skipna=True)
        * kw_factor
    )

    # ── Inverter column detection ─────────────────────────────────────────────
    # Auto-detect unless the site config explicitly supplies inverter patterns (same
    # rationale as the meter block above).
    if site_cfg is None or site_cfg.inverter_patterns is None:
        inv_kwh_matches = _auto_detect_inverter_cols(df, meter_cols[0])
        if inv_kwh_matches:
            print(
                f"[excel_loader] auto-detect '{site_id}': found {len(inv_kwh_matches)} "
                f"inverter column(s) via broad kWh scan"
            )
        else:
            raise ValueError(
                f"[excel_loader] ACE site '{site_id}': could not auto-detect inverter columns"
            )
    else:
        inv_patterns = site_cfg.inverter_patterns
        seen_nums: dict = {}
        for c in df.columns:
            c_lc = c.lower()
            for pat in inv_patterns:
                if pat.lower() in c_lc:
                    m = re.search(r"\d+", c)
                    if m:
                        num = int(m.group())
                        seen_nums.setdefault(num, []).append(c)
                    break  # one pattern match per column is enough
        inv_kwh_matches = sorted(seen_nums.items(), key=lambda x: x[0])
        if not inv_kwh_matches:
            raise ValueError(
                f"[excel_loader] ACE site '{site_id}': no inverter kWh columns found "
                f"(patterns searched: {inv_patterns})"
            )

    n_inv = len(inv_kwh_matches)
    for inv_num, kwh_cols in inv_kwh_matches:
        # Sum all source columns for this inverter number (handles multi-string inverters),
        # converting any cumulative register to per-interval energy first (per source
        # column, so one string's rollover does not zero the whole inverter), then kWh -> kW.
        kwh = sum(
            to_interval_if_cumulative(
                pd.to_numeric(df[c], errors="coerce"),
                df[config.COL_TIMESTAMP], c, site_id,
            )
            for c in kwh_cols
        )
        df[f"Inverter {inv_num} AC kW"] = (kwh * kw_factor).fillna(0.0)

    # Coerce meter phase voltage and current columns to numeric; pass through as raw
    for pat in config.ACE_METER_VOLTAGE_PATTERNS + config.ACE_METER_CURRENT_PATTERNS:
        col = _find_meter_col(df.columns, [pat])
        if col is not None:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"[excel_loader] ACE site '{site_id}': {n_inv} inverters, meter='{', '.join(meter_cols)}'")

    return df


def load_workbook(xlsx_path: Path) -> List[SiteRecord]:
    """Load every sheet from an Excel workbook and return one SiteRecord per sheet.

    site_id is read from preamble row 0 cell 0 (e.g. "Adams Farm - Built-In Query Report"
    → "Adams Farm"). Falls back to the sheet name if that cell is empty.
    Sheets missing required columns or with zero valid rows are skipped with a warning.
    """
    try:
        source = _to_transitional(xlsx_path)
        # First pass: read only preamble row 0 from each sheet to extract the site name.
        preamble: dict = pd.read_excel(source, sheet_name=None, header=None, nrows=1)
        if isinstance(source, io.BytesIO):
            source.seek(0)
        all_sheets: dict = pd.read_excel(
            source, sheet_name=None, header=_ACE_HEADER_ROW, skiprows=[_ACE_UNITS_ROW]
        )
    except Exception as exc:
        raise RuntimeError(
            f"[excel_loader] Cannot open workbook '{xlsx_path}': {exc}"
        ) from exc

    if not all_sheets:
        print(f"[excel_loader] WARNING: workbook '{xlsx_path.name}' contains no sheets")
        return []

    records = []

    for sheet_name, df in all_sheets.items():
        # Derive site_id from preamble row 0, cell 0; strip " - ..." suffix (e.g. "Adams Farm -
        # Built-In Query Report" → "Adams Farm"). Fall back to sheet name if row is empty.
        preamble_df = preamble.get(sheet_name)
        if preamble_df is not None and not pd.isna(preamble_df.iloc[0, 0]):
            raw_name = str(preamble_df.iloc[0, 0]).strip()
            site_id = raw_name.partition(" - ")[0].strip()
        else:
            site_id = sheet_name
            print(
                f"[excel_loader] WARNING: sheet '{sheet_name}' — preamble row 0 empty, "
                f"using sheet name as site_id"
            )

        if site_id not in config.SITE_CONFIGS:
            print(
                f"[excel_loader] WARNING: site '{site_id}' not found in SITE_CONFIGS — "
                f"attempting auto-detection of meter/inverter columns"
            )

        # Strip column name whitespace so comparisons against config constants work.
        df.columns = df.columns.str.strip()

        missing = _REQUIRED_ACE_RAW_COLUMNS - set(df.columns)
        if missing:
            print(
                f"[excel_loader] WARNING: skipping sheet '{site_id}' — "
                f"missing columns: {sorted(missing)}"
            )
            continue

        df = _add_ace_derived_columns(df, site_id)

        if len(df) == 0:
            print(f"[excel_loader] WARNING: skipping sheet '{site_id}' — zero rows")
            continue

        # Sort by timestamp so interval deltas are meaningful, then validate.
        df = df.sort_values(config.COL_TIMESTAMP).reset_index(drop=True)
        _validate_intervals(df, site_id)

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
