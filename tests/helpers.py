"""Synthetic-data builders for the test suite.

All fixtures are hand-built DataFrames in the *loaded* schema (Timestamp + 'Meter kW' +
derived 'Inverter N AC kW' columns, optionally original 'Inverter NN' telemetry columns and
per-phase 'PS1 IacA/B/C' columns). Nothing here reads the real data workbook, so tests are
fast, deterministic, and independent of whatever is sitting in data/raw/.
"""
import pandas as pd

from src import config

TS = config.COL_TIMESTAMP            # "Timestamp"
MK = config.COL_METER_PRODUCTION_KW  # "Meter kW"


def inv_col(n: int) -> str:
    """Derived inverter kW column the filters/calculator operate on (^Inverter \\d+ AC kW$)."""
    return f"Inverter {n} AC kW"


def at(day: int = 0, hour: int = 12, minute: int = 0) -> pd.Timestamp:
    """A timestamp `day` days into a fixed base date, at the given hour/minute."""
    return pd.Timestamp("2025-06-02 00:00:00") + pd.Timedelta(
        days=int(day), hours=int(hour), minutes=int(minute)
    )


def producing_day(day: int, n: int = 4, meter: float = 90.0,
                  inverters=(50.0, 50.0), hour: int = 11, legs=None) -> list:
    """Row-dicts for `n` producing midday intervals on `day` (meter above the night floor).

    `inverters` -> derived 'Inverter k AC kW' columns. `legs` -> a (A, B, C) tuple written to
    'PS1 IacA/B/C' so filter_phase_current sees a phase-current station.
    """
    rows = []
    for i in range(n):
        row = {TS: at(day, hour, 15 * i), MK: meter}
        for k, v in enumerate(inverters, start=1):
            row[inv_col(k)] = v
        if legs is not None:
            row["PS1 IacA"], row["PS1 IacB"], row["PS1 IacC"] = legs
        rows.append(row)
    return rows


def frame(rows: list) -> pd.DataFrame:
    """Build a DataFrame from row-dicts with a clean RangeIndex."""
    return pd.DataFrame(rows).reset_index(drop=True)
