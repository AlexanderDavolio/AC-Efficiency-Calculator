# AC Efficiency Calculator

Pipeline for computing AC efficiency and loss metrics across solar sites.

## Project layout

```
data/raw/        ← place one CSV per site here
output/          ← cleaned CSVs and summary_report.csv land here
src/
  config.py      ← column name constants and numeric thresholds
  models.py      ← SiteRecord dataclass (carries data through all stages)
  csv_loader.py  ← data source layer (swap here to use a DB or API)
  cleaners.py    ← nighttime / offline / phase-imbalance / outlier filters
  calculator.py  ← efficiency_pct and loss_delta_kw calculations
  reporter.py    ← per-site CSVs and cross-site comparison table
main.py          ← pipeline entry point
```

## Pipeline stages

```
load_all_sites()
  └─ run_all_filters()        # nighttime → offline → phase imbalance → outliers
       └─ run_all_calculations()   # efficiency_pct → loss_delta_kw
            └─ run_all_reports()   # cleaned CSVs + summary_report.csv
```

## Setup

```bash
pip install -r requirements.txt
```

## Running

Drop CSV files into `data/raw/` (one per site), then:

```bash
python main.py
```

Results appear in `output/`.

## Swapping the data source

All I/O lives in `src/csv_loader.py`. To load from a database or API, add a
new loader function there with the same return type (`List[SiteRecord]`) and
update the import in `main.py`. No other file needs to change.
