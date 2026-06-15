# Pipeline

Running `main.py` executes four sequential stages. Each stage receives data from the previous one via a shared `SiteRecord` object and logs its progress to the console.

---

## The SiteRecord Object

Every site is represented by a `SiteRecord` dataclass that flows through all four stages. It carries:

| Field | Set By | Contains |
|---|---|---|
| `site_id` | Load | Site name (sheet name for Excel, filename stem for CSV) |
| `source_path` | Load | Absolute path to the source file |
| `raw_df` | Load | The original DataFrame as parsed — never modified after this point |
| `cleaned_df` | Clean | DataFrame after all four filters have been applied |
| `enriched_df` | Calculate | DataFrame after efficiency, loss delta, and time buckets have been added |
| `summary` | Report | Dict of per-site aggregate statistics |

Keeping `raw_df`, `cleaned_df`, and `enriched_df` as separate fields (rather than overwriting in place) means you can inspect the state at any stage during debugging without re-running the pipeline.

---

## Stage 1 — Load

**Modules:** `src/excel_loader.py` (Excel workbook mode) · `src/csv_loader.py` (CSV mode)

`main.py` inspects `data/raw/` at startup and routes to the correct loader automatically:

| What's in `data/raw/` | Loader used |
|---|---|
| A `.xlsx` file | `excel_loader` — each sheet is one site |
| `.csv` files only | `csv_loader` — each file is one site |
| Both `.xlsx` and `.csv` files | `excel_loader` wins; CSVs are ignored with a warning |

**What happens (both loaders):**

1. Each site's data is read into a DataFrame. Column names are stripped of leading/trailing whitespace.
2. The timestamp column is parsed to datetime. Rows with unparseable timestamps become `NaT`.
3. All non-timestamp columns are coerced to float. Any value that cannot be converted (empty strings, `"---"`, `"N/A"`) becomes `NaN`.
4. A `SiteRecord` is created and `raw_df` is populated.

**Excel workbook specifics:** both transitional and strict OOXML formats are handled. Files saved via OneDrive or SharePoint are typically strict OOXML — the loader detects this and converts the namespaces in memory before parsing (a note is printed to the console).

The DAS export format has a two-row header: row 0 contains column names and row 1 contains units (V, A, kWh). The units row is skipped automatically. Two sets of derived columns are then computed before the `SiteRecord` is created:

- **Per-inverter AC power (kW):** `voltage × current / 1000` for each inverter (single-phase power formula). These become the `Inverter N AC kW` columns used by all downstream stages.
- **Meter average power (kW):** the per-interval energy reading (kWh) is multiplied by `60 / INTERVAL_MINUTES` to convert to average kW. The default interval is 15 minutes (configurable in `config.py`).

Sheets missing any required raw column are skipped with a warning listing the absent columns. Sheets with zero valid rows after parsing are also skipped. If the workbook file itself cannot be opened, the pipeline stops with an error. The null rate for the derived kW columns is included in the console summary so sparse sheets are immediately visible.

**CSV specifics:** if a file fails to load entirely (encoding error, wrong format), a warning is printed and that file is skipped. All other files continue normally.

**Console output to watch for:**
- Site name, row count, and date range for each successfully loaded site
- Column list — useful for verifying that expected columns parsed correctly
- A null rate line (Excel mode) showing which columns have missing values and how many
- Warning lines for any sheet or file that was skipped and why

---

## Stage 2 — Clean

**Module:** `src/cleaners.py`

Filters are applied in order. Each filter receives the DataFrame produced by the previous filter (not the original), so dropout compounds. Each filter is a pure function — it does not mutate its input, it returns a filtered copy.

### Filter 1 — Nighttime Floor

Drops rows where the meter production reading falls below the configured kilowatt threshold. This removes overnight intervals where the site is offline or producing negligible power. Keeping these rows would distort efficiency statistics since small absolute measurement errors become large relative errors near zero.

### Filter 2 — Offline Detection

Drops rows where all inverters simultaneously report zero output and the meter also reads zero or negative. This catches total-site offline events that the nighttime floor alone misses — for example, a midday grid outage where the meter reading is zero but daytime conditions would otherwise pass the floor check.

### Filter 3 — Imbalance Detection

Checks three signal groups independently for imbalance. For each group, the spread is computed per row as `(max − min) / mean` across the signals in that group. A row is dropped if it exceeds the configured threshold in **any** of the three groups.

| Group | Signals | What a high ratio indicates |
|---|---|---|
| Inverter currents | AC current at inverter 1, 2, 3 output | An inverter offline, a blown fuse, or a current sensor failure |
| Inverter voltages | AC voltage at inverter 1, 2, 3 output | Grid fault or voltage sag at one inverter's connection point |
| Inverter kW outputs | Computed AC power at inverter 1, 2, 3 | An inverter de-rated or offline while others produce normally |

Each group has its own threshold in `config.py`. Voltage is inherently tight (5% threshold) because all inverters connect to the same grid. Current and kW use a looser threshold (50%) because different inverters at a site may have different string sizes and naturally produce different outputs under normal conditions. The console output reports per-group flag counts alongside the threshold used, so you can see exactly which group is driving dropout for a given run.

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

**Module:** `main.py` (inline)

For each site, the average efficiency across all cleaned rows is printed to the console as a percentage:

```
2 Twosome Dr: 99.22%
21 Sanzari:   98.85%
```

The `src/reporter.py` module contains additional reporting functions (monthly breakdown, time-of-day summary, inverter split, Excel workbook output) that are available for use but not wired into the default run. To enable them, import and call `run_all_reports(records)` from `main.py` in place of the inline print loop.
