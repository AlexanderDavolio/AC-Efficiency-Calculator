from dataclasses import dataclass, field
import pandas as pd


@dataclass
class SiteRecord:
    """Holds all data for a single site through every pipeline stage.

    Keeping raw, cleaned, and enriched DataFrames on one object lets each
    pipeline stage receive and return the same type, and makes it easy to
    inspect intermediate state during debugging.
    """

    site_id: str
    # Path to the source CSV on disk.
    source_path: str
    # DataFrame as loaded directly from CSV — never mutated after load.
    raw_df: pd.DataFrame = field(repr=False, default=None)
    # DataFrame after all cleaning filters have been applied.
    cleaned_df: pd.DataFrame = field(repr=False, default=None)
    # DataFrame after efficiency and loss columns have been added.
    enriched_df: pd.DataFrame = field(repr=False, default=None)
    # Per-site summary statistics produced by the reporter.
    summary: dict = field(default_factory=dict)
