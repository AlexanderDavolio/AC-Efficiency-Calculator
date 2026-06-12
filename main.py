"""Entry point — runs the full AC efficiency pipeline.

Pipeline order:
    1. LOAD    — discover and read all site CSVs into SiteRecord objects
    2. CLEAN   — apply nighttime / offline / phase-imbalance / outlier filters
    3. CALCULATE — add efficiency_pct and loss_delta_kw columns
    4. REPORT  — write cleaned CSVs, per-site summaries, and comparison table
"""

from src.csv_loader import load_all_sites
from src.cleaners import run_all_filters
from src.calculator import run_all_calculations
from src.reporter import run_all_reports


def main() -> None:
    """Orchestrate the four pipeline stages end to end."""

    # ── 1. LOAD ──────────────────────────────────────────────────────────────
    # Reads every CSV in data/raw/ and wraps each in a SiteRecord.
    # Swap load_all_sites for a different loader here to change the data source.
    records = load_all_sites()

    # ── 2. CLEAN + 3. CALCULATE ───────────────────────────────────────────────
    # Filters operate on DataFrames; results are stored back on the SiteRecord.
    for r in records:
        r.cleaned_df = run_all_filters(r.raw_df)
        r.enriched_df = run_all_calculations(r.cleaned_df)

    # ── 4. REPORT ────────────────────────────────────────────────────────────
    # Writes cleaned CSVs and the cross-site summary table to output/.
    run_all_reports(records)


if __name__ == "__main__":
    main()
