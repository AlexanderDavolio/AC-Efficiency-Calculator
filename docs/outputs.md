# Outputs

A completed run produces two types of output in the `output/` directory: a cleaned CSV per site and a single multi-tab Excel workbook covering all sites.

---

## Per-Site Cleaned CSV

**File:** `output/<site_id>_cleaned.csv`

This is the row-level dataset after cleaning and enrichment. It contains every row that survived all four filters, with three groups of columns:

**Original columns** — all columns from the raw CSV, exactly as loaded (column names whitespace-stripped). Extra columns from your DAS export are preserved here.

**Derived columns added by the calculator:**

| Column | What It Contains |
|---|---|
| Total inverter power | Sum of all per-inverter AC power readings for that row |
| Efficiency percentage | Meter reading divided by total inverter power, expressed as a percentage |
| Loss delta | Total inverter power minus meter reading, in kilowatts |
| Month | Integer 1–12 extracted from the timestamp |
| Time-of-day bucket | Categorical label (Morning, Peak, Afternoon, Other) based on the hour of the timestamp |

Use this file when you need row-level detail — for example, to build custom pivot tables, feed into a separate analysis, or investigate a specific date range.

---

## Multi-Tab Excel Report (`efficiency_report.xlsx`)

### Summary Tab

One row per site. This is the first tab to check after a run.

| Column | What to Look For |
|---|---|
| Site name | Matches the CSV filename stem |
| Raw row count | Total rows before cleaning — useful for confirming the full export was loaded |
| Clean row count | Rows remaining after all filters |
| Clean percentage | Fraction retained — healthy sites typically retain a majority of daytime rows; unusually low retention may indicate a threshold is mistuned or the export contains a lot of off-hours data |
| Average efficiency | The primary metric — see interpretation guidance below |
| Min efficiency | Lowest single-row efficiency in the cleaned dataset; a very low min that survived filtering may warrant spot-checking |
| Max efficiency | Highest single-row efficiency; values meaningfully above 100% that survived the outlier filter suggest meter calibration drift |
| Average loss delta | Mean kW gap between inverters and meter; positive is expected, negative is a flag |
| Date range | Start and end timestamps — verify this matches your intended analysis period |

Sites are sorted by average efficiency descending, so underperformers appear at the bottom.

---

### Monthly Tab

One row per site per calendar month. Use this tab to identify:

- **Seasonal trends** — efficiency may vary by month due to temperature effects on inverter conversion, irradiance angle effects on string voltage, or seasonal curtailment patterns
- **Degradation over multi-year datasets** — if you load multiple years of the same site, comparing the same month across years reveals year-over-year changes
- **Anomalous months** — a single month with materially lower efficiency than adjacent months may correspond to a known event (equipment swap, extended curtailment, grid issue) or an unknown one worth investigating

Columns mirror the summary tab but are scoped to each month: row count, efficiency avg/min/max, and average loss delta.

---

### Time of Day Tab

One row per site per time bucket (Morning, Peak, Afternoon). "Other" rows are excluded from this tab. Use this tab to identify:

- **Time-of-day efficiency patterns** — inverters often run at higher efficiency near peak irradiance; a site where Morning efficiency is significantly lower than Peak may have shading or string-level issues in early hours
- **Afternoon degradation** — some inverter models de-rate in high ambient temperatures; a consistent efficiency drop in the Afternoon bucket relative to Peak is worth cross-referencing against temperature data
- **Asymmetric loss deltas** — if the loss delta is much larger in one time bucket than others, it may point to a specific operating regime (e.g., high-irradiance curtailment, reactive power dispatch) where losses are concentrated

The three buckets map to morning ramp, midday peak, and afternoon shoulder periods. Exact hour boundaries are defined in `src/calculator.py`.

---

### Inverter Split Tab

One row per site with two sets of metrics for each inverter: average power share (as a percentage of total inverter output) and average power output in kilowatts.

**What an imbalance signals:**

In a well-matched system, both inverters should contribute roughly equally to total output. A persistent imbalance (one inverter consistently producing significantly less than the other as a share of total) may indicate:

- String count or string sizing differences between the two inverter inputs (expected and benign if by design)
- One inverter operating at a lower MPP tracking efficiency due to shading, soiling, or a string fault
- A hardware issue (degraded capacitors, fan failure causing thermal de-rating) on the underperforming inverter
- A communication or sensor fault where one inverter's reported output does not reflect actual output

Note that a power share imbalance does not by itself imply a problem — cross-reference with the actual kW values and the system design to determine whether the split is within expected bounds.

---

## Interpreting Efficiency Percentage

Efficiency is computed as **meter reading / total inverter output × 100**.

This is not inverter conversion efficiency (DC-to-AC). The inverters are already on the AC side. This metric captures **AC-side delivery efficiency** — how much of the power the inverters put onto the AC bus reaches the production meter.

Expected losses between the inverter AC output and the production meter include:
- Transformer copper and iron losses
- AC wiring resistance losses
- Switchgear and protection relay parasitic draw

For a typical utility-scale or C&I site, a well-performing system should show average efficiency comfortably above 95% on a cleaned dataset.

| Range | Interpretation |
|---|---|
| Above ~98% | Healthy — losses are within normal expectations |
| ~95–98% | Marginal — may be acceptable depending on transformer design; worth monitoring for trend |
| Below ~95% | Warrants investigation — losses are above typical; check transformer health, wiring connections, and meter calibration |
| Consistently above 100% | Meter likely reading high relative to inverters — check meter calibration, CT ratio, or measurement timing offset |

**Important:** These ranges are illustrative guidance, not hard thresholds. The right reference point is the site's own historical baseline or its design documents, not an industry-wide benchmark.

---

## What a Healthy Result Looks Like

- Cleaning retains the majority of daytime rows with consistent dropout across months
- Average efficiency is stable and high across months and time-of-day buckets
- Loss delta is consistently positive and small relative to total output
- Both inverters contribute roughly equal shares of total output (absent a design asymmetry)
- No single month or time bucket shows a sharp anomaly

## What Warrants Investigation

- Cleaning retains an unusually small fraction of rows (check thresholds and the raw data)
- Average efficiency is below expected levels for this site type
- Loss delta is negative on average (meter reads higher than inverters)
- One inverter consistently contributes significantly less than the other
- A specific month or time bucket shows a sharp efficiency drop not present in adjacent periods
- Min efficiency values deep below the outlier filter floor that survived cleaning
