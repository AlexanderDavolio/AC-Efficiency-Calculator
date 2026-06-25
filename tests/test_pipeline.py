"""Reporter roll-up invariants and end-to-end golden-master checks through the full pipeline."""
import numpy as np
import pytest

from src import config, cleaners, calculator, reporter
from src.calculator import run_all_calculations
from src.models import SiteRecord
from helpers import TS, MK, inv_col, at, producing_day, frame


def _record(df, site_id="t"):
    rec = SiteRecord(site_id=site_id, source_path="x", raw_df=df)
    rec.cleaned_df = cleaners.run_all_filters(rec.raw_df, site_id)
    rec.enriched_df = run_all_calculations(rec.cleaned_df)
    return rec


def test_reported_efficiency_is_energy_weighted_not_mean_of_ratios():
    # (180,200)->90% and (10,100)->10%. Mean of per-interval ratios = 50%.
    # Energy-weighted (sum first) = 100*(180+10)/(200+100) = 63.33%. They must differ.
    df = frame([
        {TS: at(0, 12, 0),  MK: 180.0, inv_col(1): 200.0},
        {TS: at(0, 12, 15), MK: 10.0,  inv_col(1): 100.0},
    ])
    enr = run_all_calculations(df)
    weighted = reporter._weighted_efficiency_pct(enr)
    assert weighted == pytest.approx(100 * 190 / 300)              # 63.33 — sum-first
    assert abs(weighted - enr[config.COL_EFFICIENCY_PCT].mean()) > 10.0  # NOT the 50% mean-of-ratios


def test_weighted_sums_pair_meter_and_inverter_on_defined_intervals():
    # the inv-zero row has NaN efficiency and must drop out of BOTH sums, not just the numerator.
    df = frame([
        {TS: at(0, 12, 0),  MK: 90.0, inv_col(1): 100.0},   # counted
        {TS: at(0, 12, 15), MK: 90.0, inv_col(1): 0.0},     # eff NaN -> excluded from both sums
    ])
    enr = run_all_calculations(df)
    assert reporter._weighted_efficiency_pct(enr) == pytest.approx(90.0)


def test_pipeline_golden_normal_site():
    rows = []
    for d in range(5):
        rows += producing_day(d, n=4, meter=90.0, inverters=(50.0, 50.0))
    s = reporter.summarise_site(_record(frame(rows)))
    assert s["good_days"] == 5
    assert s["bad_days"] == 0
    assert s["avg_efficiency_pct"] == pytest.approx(90.0)


def test_pipeline_daytime_only_site_uses_daytime_denominator():
    # Night rows have meter > floor (standby) but NULL inverter telemetry — under the old
    # producing denominator they would pollute every day to ~40% clean (all bad). With daytime
    # detection + scoping, only the reporting daytime intervals count, so all 5 days are good.
    rows = []
    for d in range(5):
        for h in range(0, 6):
            rows.append({TS: at(d, h), MK: 2.0, "Inverter 01": np.nan, inv_col(1): 0.0})
        for i in range(4):
            rows.append({TS: at(d, 11, 15 * i), MK: 90.0, "Inverter 01": 50.0, inv_col(1): 100.0})
    df = frame(rows)
    assert calculator.is_daytime_only_telemetry(df) is True
    # Contrast at the engine level: producing denominator gives 0 good days, daytime gives 5.
    rec = _record(df)
    kept = rec.enriched_df.index
    assert calculator.count_good_days(df, kept, daytime_only=False) == 0
    assert calculator.count_good_days(df, kept, daytime_only=True) == 5
    assert reporter.summarise_site(rec)["good_days"] == 5
