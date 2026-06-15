# Pipeline

Running `main.py` executes four sequential stages. Each stage receives data from the previous one via a shared `SiteRecord` object and logs its progress to the console.

---

## The SiteRecord Object

Every site is represented by a `SiteRecord` dataclass that flows through all four stages. It carries:

| Field | Set By | Contains |
|---|---|---|
| `site_id` | Load | Site name derived from the CSV filename stem |
| `source_path` | Load | Absolute path to the source CSV |
| `raw_df` | Load | The original DataFrame as parsed — never modified after this point |
| `cleaned_df` | Clean | DataFrame after all four filters have been applied |
| `enriched_df` | Calculate | DataFrame after efficiency, loss delta, and time buckets have been added |
| `summary` | Report | Dict of per-site aggregate statistics |

Keeping `raw_df`, `cleaned_df`, and `enriched_df` as separate fields (rather than overwriting in place) means you can inspect the state at any stage during debugging without re-running the pipeline.

---

## Stage 1 — Load

**Module:** `src/csv_loader.py`

**What happens:**

1. The loader scans `data/raw/` and finds all files with a `.csv` extension.
2. For each file, it reads the CSV, strips leading/trailing whitespace from all column names, and attempts to parse the timestamp column as a datetime. Rows with unparseable timestamps become `NaT`.
3. All non-timestamp columns are coerced to float. Any value that cannot be converted (empty strings, `"---"`, `"N/A"`) becomes `NaN`.
4. A `SiteRecord` is created and `raw_df` is populated.
5. If a file fails to load entirely (encoding error, wrong format), a warning is printed and that file is skipped. All other files continue normally.

**Console output to watch for:**
- File name and row count for each successfully loaded site
- The date range of the loaded data (first and last timestamp)
- Column list — useful for verifying that expected columns parsed correctly
- A warning line for any file that was skipped

---

## Stage 2 — Clean

**Module:** `src/cleaners.py`

Filters are applied in order. Each filter receives the DataFrame produced by the previous filter (not the original), so dropout compounds. Each filter is a pure function — it does not mutate its input, it returns a filtered copy.

### Filter 1 — Nighttime Floor

Drops rows where the meter production reading falls below the configured kilowatt threshold. This removes overnight intervals where the site is offline or producing negligible power. Keeping these rows would distort efficiency statistics since small absolute measurement errors become large relative errors near zero.

### Filter 2 — Offline Detection

Drops rows where all inverters simultaneously report zero output and the meter also reads zero or negative. This catches total-site offline events that the nighttime floor alone misses — for example, a midday grid outage where the meter reading is zero but daytime conditions would otherwise pass the floor check.

### Filter 3 — Phase Imbalance

Checks three signal groups independently for imbalance. For each group, the spread is computed per row as `(max − min) / mean` across the signals in that group. A row is dropped if it exceeds the configured threshold in **any** of the three groups.

| Group | Signals | What a high ratio indicates |
|---|---|---|
| Phase currents | AC current on phases A, B, C | Grid fault, open-phase condition, or a current sensor reporting incorrectly |
| Phase voltages | Line-to-neutral voltage on phases A, B, C | Voltage sag or swell on one phase, upstream grid imbalance |
| Inverter outputs | AC power from inverter 1 and inverter 2 | MPPT divergence, string fault, or hardware de-rating on one inverter |

Each group has its own threshold constant in `config.py` because normal operating spread differs by signal type — voltage is inherently tighter than inverter power output under normal conditions. The console output reports per-group flag counts alongside the threshold used for each, so you can see exactly which group is driving dropout for a given run.

Rows where all signals in a group read zero produce an undefined ratio and pass through as `NaN`. They will have been caught by the offline filter in a normal run.

### Filter 4 — Gross Outlier Bounds

Computes efficiency inline as meter output divided by total inverter output, expressed as a percentage. Rows outside the configured `[min, max]` window are dropped. Values below the minimum indicate an implausibly large loss that is more likely a data artifact than a real operating condition. Values above the maximum indicate the meter is reading higher than the inverters, which is physically implausible under normal conditions and typically signals a measurement timing mismatch or calibration drift.

**Console output to watch for:**
- Rows remaining after each individual filter, with the count dropped and percentage relative to the pre-filter row count
- Total row count before and after all four filters combined, with overall dropout percentage
- A high dropout rate (>60%) may indicate a threshold is misconfigured for this site or that the data export contains significant off-hours data

---

## Stage 3 — Calculate

**Module:** `src/calculator.py`

Three calculations run in dependency order on the cleaned DataFrame. Each returns a copy with new columns appended.

### Efficiency and Inverter Total

Sums the per-inverter power readings into a total inverter output column, then divides the meter reading by that total to produce an efficiency percentage. Rows where the inverter total is zero or negative produce `NaN` in both derived columns — these are edge cases not caught by earlier filters (e.g., a single row where one inverter reads slightly positive and the other slightly negative, netting near zero).

### Loss Delta

Subtracts the meter reading from the total inverter output to produce a loss delta in kilowatts. A positive delta means power was generated but not delivered — expected for normal wiring and transformer losses. A negative delta means the meter reads higher than the inverters, which warrants investigation.

### Time Buckets

Extracts the hour from each timestamp and assigns two derived columns:
- **Month number** — integer 1–12, used to group rows in the monthly summary
- **Time-of-day bucket** — a categorical label (Morning, Peak, Afternoon, Other) based on the hour of day. The exact hour boundaries for each bucket are defined in `calculator.py`. Rows outside the active generation window (e.g., very early morning or late evening rows that passed the nighttime floor) fall into "Other" and are excluded from the time-of-day summary.

**Console output to watch for:**
- Average efficiency across all rows — a quick sanity check that the cleaning stage produced a reasonable dataset
- Average loss delta — should be a small positive number under normal conditions; a negative average is a strong signal that something is wrong with the data or the site

---

## Stage 4 — Report

**Module:** `src/reporter.py`

### Per-Site Cleaned CSV

For each site, the enriched DataFrame (all cleaned rows plus all derived columns) is written to `output/<site_id>_cleaned.csv`. This file contains the full row-level detail used to produce the summary statistics.

### Excel Workbook

A single workbook covering all sites in the run is written to `output/efficiency_report.xlsx`. It contains four sheets:

1. **Summary** — one row per site, key aggregate metrics
2. **Monthly** — one row per site per month, efficiency and loss delta statistics
3. **Time of Day** — one row per site per time bucket (Morning, Peak, Afternoon), efficiency and loss delta statistics
4. **Inverter Split** — one row per site, average power share and average output for each inverter

Column widths are auto-fitted. On Windows, the workbook opens automatically when the run completes.

**Console output to watch for:**
- Confirmation that the cleaned CSV was written for each site
- The path to the Excel workbook
- Any per-site summary statistics printed during report generation
