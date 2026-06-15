# Inputs

## Where to Place Files

Drop all input files into the `data/raw/` directory at the project root. The tool auto-detects whether to use Excel or CSV mode based on what it finds there — no config change required.

**Excel workbook mode (preferred for multi-site runs):** place a single `.xlsx` file in `data/raw/`. Each sheet in the workbook is treated as one site; the sheet name becomes the site identifier.

```
AC-Efficiency Calculator/
└── data/
    └── raw/
        └── sites.xlsx          ← sheet per site
```

**CSV mode:** place one `.csv` file per site in `data/raw/`. The filename stem (everything before `.csv`) becomes the site identifier.

```
AC-Efficiency Calculator/
└── data/
    └── raw/
        ├── site_a.csv
        ├── site_b.csv
        └── ...
```

If both a `.xlsx` file and `.csv` files are present, the `.xlsx` takes precedence and the CSVs are ignored (a warning is printed).

---

## File Format

### Excel workbook

One workbook, one sheet per site. The sheet name is used as the site identifier in all reports — use a name that is meaningful, such as the site name or SCADA asset ID. Avoid special characters that are invalid in Excel sheet names or file paths.

Sheets that are missing any required column are skipped automatically with a warning; other sheets in the same workbook continue to process normally.

### CSV files

One CSV per site. The filename stem becomes the site identifier. There is no enforced naming convention beyond the `.csv` extension.

**One file per site per run.** If you have multiple years of data for the same site, either combine them into a single export or use separate filenames (e.g., `site_a_2024.csv`, `site_a_2025.csv`). Separate files produce separate rows in the output reports.

---

## Required Columns

The tool expects the following measurement streams in the CSV. Column names are mapped in `src/config.py` — see [configuration.md](configuration.md) if your DAS uses different headers.

| Concept | What It Represents |
|---|---|
| **Timestamp** | Local site time for each measurement interval. Should be parseable as a datetime (e.g., ISO 8601 or common US formats). |
| **Production meter — active power** | Site-level active power at the point of interconnection, in kilowatts. This is the "delivered" side of the efficiency calculation. |
| **Inverter 1 — AC power** | Active power output of inverter 1, in kilowatts. |
| **Inverter 2 — AC power** | Active power output of inverter 2, in kilowatts. |
| **Phase A current** | AC current on phase A, used for imbalance detection. |
| **Phase B current** | AC current on phase B, used for imbalance detection. |
| **Phase C current** | AC current on phase C, used for imbalance detection. |
| **Phase A voltage** | Line-to-neutral voltage on phase A, used for imbalance detection. |
| **Phase B voltage** | Line-to-neutral voltage on phase B, used for imbalance detection. |
| **Phase C voltage** | Line-to-neutral voltage on phase C, used for imbalance detection. |

---

## What Happens If a Column Is Missing or Malformed

**Missing column:** The pipeline will raise a `KeyError` at the first stage that references that column. The error message will include the column name as defined in `config.py`. Check that your CSV headers match the names configured there (after whitespace stripping — the loader trims leading/trailing spaces from all column names automatically).

**Malformed numeric values:** Any non-numeric value in a numeric column (e.g., `"---"`, `"N/A"`, empty string) is silently coerced to `NaN` by the loader. Rows with `NaN` in columns used by a filter are typically dropped or passed through depending on the filter logic — see [pipeline.md](pipeline.md) for details.

**Malformed timestamps:** Rows where the timestamp cannot be parsed are set to `NaT`. These rows are not explicitly filtered out but will produce `NaN` in the month and time-bucket columns added by the calculator. They will appear in the cleaned CSV but may distort the monthly summary if there are many of them.

**Completely unparseable file:** If a CSV fails to load at all (e.g., wrong encoding, binary content, completely wrong structure), the loader logs a warning and skips that file. Other sites in the same run are unaffected. For Excel workbooks, if the file itself cannot be opened the pipeline stops with a clear error; if an individual sheet is missing required columns or has zero valid rows, that sheet is skipped and the rest of the workbook continues.

---

## Tips for Preparing DAS Exports

**Column name whitespace:** DAS platforms often export headers with trailing spaces or mixed case. The loader strips leading/trailing whitespace automatically, but it does not normalize case. If your headers use different casing than what is configured, update `config.py` — do not rename the CSV headers manually, as that creates a fragile manual step.

**Timestamp format:** Most standard datetime formats parse correctly. If you see a large number of `NaT` values in the timestamp column after loading, check whether the DAS export uses a non-standard format (e.g., Unix epoch, Julian day). The loader uses pandas' default datetime parser; custom formats can be added in `csv_loader.py` if needed.

**Export interval:** The tool is interval-agnostic — it works on whatever granularity the DAS exports (1-minute, 5-minute, 15-minute). Thresholds in `config.py` are expressed in absolute kW values and ratios, not normalized per interval, so coarser intervals do not require threshold adjustments.

**Column count:** Extra columns beyond the required set are loaded and passed through to the cleaned CSV without modification. There is no need to trim the export to only the columns the tool uses.

**Multi-site exports:** If your DAS exports all sites into a single file, you have two options. The simpler path is to use Excel workbook mode: paste each site's data into its own sheet in a single `.xlsx` and drop that into `data/raw/`. Alternatively, split into per-site CSVs using a `groupby` + `to_csv` in pandas.
