# AC Efficiency Calculator

## Project Overview

This pipeline measures AC-side efficiency across Altus Power solar sites by comparing
inverter AC output to production meter readings. For each 15-minute interval, it
computes the fraction of inverter-generated power that actually reaches the meter
(efficiency %) and quantifies the delta between inverter total and meter reading
(loss kW / kWh). Results are broken down by site, month, and time of day to identify
underperforming sites, systematic losses, and trends over time.

## How It Works

| File | Role |
|---|---|
| `src/config.py` | Global constants and thresholds — column names, interval length, cleaning bounds, imbalance tolerances. Edit here when data schema or business rules change. |
| `src/excel_loader.py` | Reads AlsoEnergy ACE workbooks. Auto-detects the production meter column and per-inverter kWh columns for each sheet. Converts kWh to average kW, handles strict OOXML format transparently. |
| `src/cleaners.py` | Filters nighttime rows (any inverter at zero), comms dropouts, and gross outliers (efficiency outside 80–100%). |
| `src/calculator.py` | Computes `efficiency_pct` (meter kW / inverter total kW × 100), `loss_delta_kw`, `loss_pct`, `energy_lost_kwh`, and time-of-day buckets. |
| `src/reporter.py` | Prints terminal output: per-site monthly efficiency tables, inverter power split, cross-site summary, and a phase imbalance sensitivity table. Writes a cleaned CSV per site to `output/`. |
| `main.py` | Entry point. Loads the workbook, runs cleaners → calculator → reporter in order. Accepts an optional `--site` flag to process a single site. |

## Input Format

The pipeline expects **AlsoEnergy ACE Built-In Query Report** format:

- Excel workbook (`.xlsx`), one sheet per site
- Rows 0–3: preamble (title, date range, blank)
- Row 4: column headers
- Row 5: units row (skipped automatically)
- Row 6+: 15-minute interval data

### Column types

| Category | Examples | Type | Units | Required |
|---|---|---|---|---|
| Timestamp | `Timestamp` | datetime | — | Yes |
| Generation by Inverter | `INVERTER 1-1`, `Sungrow 60KW Inverter - A1` | numeric float | kWh per interval | Yes |
| Production Meter | `Wattnode Meter`, `SEL-735`, `METER - PRODUCTION`, `Production Meter` | numeric float | kWh per interval | Yes |
| Production Meter Phase — Voltage | `VacA`, `VacB`, `VacC` | numeric float | Volts (V) | No — needed for phase imbalance analysis |
| Production Meter Phase — Current | `IacA`, `IacB`, `IacC` | numeric float | Amps (A) | No — needed for phase imbalance analysis |

**Generation by Inverter** and **Production Meter** columns must be **per-interval energy (kWh)**, not instantaneous power (kW) or cumulative totals. The pipeline converts them to average kW using `kWh × (60 / 15)`.

The pipeline auto-detects the meter and inverter columns by keyword — the exact column names do not need to match the examples above as long as the header contains a recognisable keyword (`inverter`, `meter`, `production`, etc.).

Place the workbook in `data/raw/`.

## How To Run

```bash
pip install -r requirements.txt
python main.py                   # process all sites in the workbook
python main.py --site "Adams Farm"   # process a single site
```

Output CSVs land in `output/<site_name>_cleaned.csv`.

## Known Data Requirements

- **Inverter columns must be interval kWh, not cumulative lifetime energy.** Columns
  that accumulate over time (daily running totals, lifetime totals) will produce
  incorrect kW values. The pipeline does not attempt to detect or correct cumulative
  columns — export interval energy from AlsoEnergy, not cumulative energy.

- **A true point-of-interconnection (POI) meter is required.** The pipeline computes
  efficiency as meter ÷ inverter total. Sites where the "meter" column is not a real
  revenue-grade POI meter (e.g. it is estimated generation or a clamp meter inside the
  array) will produce results that are not meaningful and should be excluded.
