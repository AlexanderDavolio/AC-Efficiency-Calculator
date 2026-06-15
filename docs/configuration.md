# Configuration

All tunable parameters live in `src/config.py`. No other file needs to change when you add a new site, adjust a threshold, or map to different CSV headers. The pipeline reads everything from this file at import time.

---

## Column Name Mappings

Config defines a constant for every column the pipeline reads or writes. These constants are used throughout `cleaners.py`, `calculator.py`, and `reporter.py` — never raw strings.

**What to change when your CSV headers differ:**

If your DAS exports use different column headers than the defaults, update the corresponding constant in `config.py`. For example, if your meter column is labeled differently than the default, find the meter production constant and update its value to match your export header exactly (case-sensitive, after whitespace stripping).

The constants cover:

| Category | Constants |
|---|---|
| **Input columns** | Timestamp, meter production power, inverter 1 AC power, inverter 2 AC power, phase A/B/C currents, phase A/B/C voltages |
| **Derived columns** | Total inverter power, efficiency percentage, loss delta, month number, time-of-day bucket |

Derived column constants control what appears as headers in the cleaned CSV and in Excel report columns. Renaming them here renames them everywhere.

---

## Cleaning Thresholds

Four thresholds control the filtering stage. Each is described below with guidance on when to tighten or loosen it.

### 1. Nighttime Floor

**What it controls:** The minimum meter production value (in kW) below which a row is treated as a nighttime or offline reading and dropped.

**Default behavior:** Rows where the meter reads below this floor are excluded from all analysis.

**When to loosen it (lower the value):**
- If your site produces very low power at dawn/dusk and you want to include those ramp periods
- If you are analyzing curtailment events where the meter genuinely reads near zero during daylight

**When to tighten it (raise the value):**
- If low-irradiance morning/evening intervals are adding noise to efficiency calculations
- If your site has a significant transformer no-load draw that makes near-zero readings uninformative

---

### 2. Offline Detection

This filter has no numeric threshold — it is a logical condition. A row is dropped when **all** inverters simultaneously report zero output **and** the meter also reads zero or negative. This catches total-site offline events (grid outages, scheduled maintenance, sensor faults) that the nighttime floor alone would not remove.

**When to adjust:**
This filter's logic is in `cleaners.py` rather than a config constant because it is a boolean condition rather than a numeric cutoff. If your site has more than two inverters, the filter condition will need to be extended to include the additional inverter columns.

---

### 3. Phase Imbalance Ratios

**What it controls:** The maximum allowed spread within each of three signal groups. Spread is computed per row as `(max − min) / mean` across the signals in that group. Each group has its own threshold because normal operating variation differs by signal type.

| Signal group | Signals checked | Threshold constant |
|---|---|---|
| Phase currents | AC current on phases A, B, and C | `CURRENT_IMBALANCE_THRESHOLD` |
| Phase voltages | Line-to-neutral voltage on phases A, B, and C | `VOLTAGE_IMBALANCE_THRESHOLD` |
| Inverter outputs | AC power from inverter 1 and inverter 2 | `INVERTER_IMBALANCE_THRESHOLD` |

A row is dropped if it exceeds the threshold in **any** of the three groups. The three checks are independent — the console output shows per-group flag counts so you can see which group is driving dropout.

**When to loosen a threshold (raise the ratio):**
- If your site has a known structural asymmetry in that signal group (e.g., unequal string counts between inverters, or a single-phase load on the AC bus affecting one current phase)
- If you are consistently losing too many valid rows and the imbalance is not correlated with other anomalies

**When to tighten a threshold (lower the ratio):**
- If you want to be more aggressive about flagging fault conditions in a specific signal group
- If downstream analysis is sensitive to imbalance effects on the metric being studied

**Edge case:** Rows where all signals in a group read zero produce an undefined ratio (division by zero). These are converted to `NaN` and pass through the imbalance check — they will be caught by the nighttime floor or offline filter instead.

---

### 4. Gross Outlier Bounds

**What it controls:** The minimum and maximum efficiency percentage a row can have and still be retained. Efficiency is computed inline as `meter kW / total inverter kW × 100`. Rows outside the `[min, max]` window are dropped.

**Default behavior:** A tight window around physically plausible AC efficiency values. Values slightly above 100% are allowed for the max to account for meter calibration offsets and measurement timing jitter.

**When to loosen it (widen the window):**
- If legitimate operating conditions at your site produce efficiency values outside the defaults (e.g., very long AC cable runs with documented higher losses would push min down)
- During initial data exploration, temporarily widening the window lets you see the full distribution before choosing a tighter cutoff

**When to tighten it (narrow the window):**
- If you want stricter data quality and are confident your site should operate within a narrower band
- If the cleaned dataset still shows anomalous spikes in efficiency and you want to exclude them

**Edge case:** Rows where total inverter output is zero or negative produce an undefined efficiency ratio. These become `NaN` and pass through this filter — they will have been caught by the offline or nighttime filters first in a normal run.

---

## Adding a New Site

No code changes are required. Drop a new CSV into `data/raw/` and run `main.py`. The loader discovers all CSVs in that directory on startup. The new site appears as a new row in the summary report and contributes its data to the monthly, time-of-day, and inverter split sheets.

The only case where a new site requires a config change is if its DAS export uses different column headers than the currently mapped defaults. In that case, either:

1. **Standardize the headers** in the CSV export settings of your DAS (preferred — keeps config clean)
2. **Update config.py** to match the new headers (only viable if all sites share the same export format)

If you need to support sites with fundamentally different column schemas in the same run, that would require refactoring the config to support per-site column maps — currently out of scope.
