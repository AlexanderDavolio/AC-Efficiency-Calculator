"""Efficiency/loss maths, daytime-only telemetry detection, and the single-source-of-truth
good-day classification (calculator.day_quality / count_good_days)."""
import numpy as np
import pytest

from src import config, calculator
from helpers import TS, MK, inv_col, at, producing_day, frame


# ── efficiency / loss ──────────────────────────────────────────────────────────

def test_calculate_efficiency_and_total():
    df = frame([{TS: at(0, 12), MK: 90.0, inv_col(1): 60.0, inv_col(2): 40.0}])
    out = calculator.calculate_efficiency(df)
    assert out[config.COL_TOTAL_INVERTER_KW].iloc[0] == 100.0
    assert out[config.COL_EFFICIENCY_PCT].iloc[0] == pytest.approx(90.0)


def test_efficiency_is_nan_when_inverter_total_nonpositive():
    df = frame([{TS: at(0, 12), MK: 90.0, inv_col(1): 0.0, inv_col(2): 0.0}])
    out = calculator.calculate_efficiency(df)
    assert np.isnan(out[config.COL_EFFICIENCY_PCT].iloc[0])


def test_loss_delta_and_energy():
    df = frame([{TS: at(0, 12), MK: 90.0, inv_col(1): 100.0}])
    out = calculator.calculate_loss_delta(calculator.calculate_efficiency(df))
    assert out[config.COL_LOSS_DELTA_KW].iloc[0] == pytest.approx(10.0)
    assert out[config.COL_ENERGY_LOST_KWH].iloc[0] == pytest.approx(10.0 * config.INTERVAL_MINUTES / 60)


# ── daytime-only telemetry detection ─────────────────────────────────────────────

def _detection_frame(night_value):
    """2 days: night hours 0-5 (original inverter cols set to night_value) + 4 midday intervals."""
    rows = []
    for d in range(2):
        for h in range(0, 6):
            rows.append({TS: at(d, h), "Inverter 01": night_value, "Inverter 02": night_value, MK: 0.0})
        for i in range(4):
            rows.append({TS: at(d, 12, 15 * i), "Inverter 01": 50.0, "Inverter 02": 50.0, MK: 90.0})
    return frame(rows)


def test_daytime_only_true_when_night_is_null():
    assert calculator.is_daytime_only_telemetry(_detection_frame(np.nan)) is True


def test_daytime_only_false_when_night_records_zero():
    # A normal site records 0 at night (present), not NULL — must NOT be flagged.
    assert calculator.is_daytime_only_telemetry(_detection_frame(0.0)) is False


def test_daytime_only_false_without_original_inverter_columns():
    df = frame([{TS: at(0, h), MK: 0.0} for h in range(0, 6)])
    assert calculator.is_daytime_only_telemetry(df) is False


def test_daytime_only_ignores_derived_kw_columns():
    # derived 'Inverter N AC kW' columns are NULL-collapsed at load, so they must not be the
    # basis for detection — a site with only derived columns is never daytime-only.
    rows = []
    for h in range(0, 6):
        rows.append({TS: at(0, h), inv_col(1): 0.0, MK: 0.0})
    assert calculator.is_daytime_only_telemetry(frame(rows)) is False


def test_reporting_mask_marks_daytime_only():
    df = _detection_frame(np.nan)
    mask = calculator.telemetry_reporting_mask(df)
    night = df[TS].dt.hour.between(0, 5)
    assert not mask[night].any()      # night = all-null = not reporting
    assert mask[~night].all()         # day = present = reporting


# ── good-day classification (single source of truth) ─────────────────────────────

def test_day_quality_classifies_good_and_bad():
    rows = producing_day(0, n=5) + producing_day(1, n=5)   # idx 0-4 day0, 5-9 day1
    df = frame(rows)
    kept = [0, 1, 2, 3, 5, 6]                               # day0 4/5=0.8 good, day1 2/5=0.4 bad
    dq = calculator.day_quality(df, kept)
    assert len(dq) == 2
    assert int(dq["good"].sum()) == 1
    assert calculator.count_good_days(df, kept) == 1


def test_day_quality_excludes_nighttime_meter():
    rows = producing_day(0, n=4) + [{TS: at(0, 3), MK: 0.0, inv_col(1): 0.0}]
    df = frame(rows)
    dq = calculator.day_quality(df, [0, 1, 2, 3])
    assert dq["n_prod"].iloc[0] == 4          # the meter<=floor night row is not in the denominator
    assert bool(dq["good"].iloc[0]) is True


def test_day_quality_daytime_scoping_flips_classification():
    # 8 producing intervals, 4 with NULL inverter telemetry (non-reporting); the 4 reporting
    # intervals are clean. Without scoping that's 4/8 (bad); with scoping 4/4 (good).
    rows = [{TS: at(0, 11, 15 * i), MK: 90.0, "Inverter 01": (50.0 if i < 4 else np.nan)}
            for i in range(8)]
    df = frame(rows)
    kept = [0, 1, 2, 3]
    assert bool(calculator.day_quality(df, kept, daytime_only=False)["good"].iloc[0]) is False
    assert bool(calculator.day_quality(df, kept, daytime_only=True)["good"].iloc[0]) is True


def test_count_good_days_matches_day_quality():
    rows = producing_day(0, n=4) + producing_day(1, n=4) + producing_day(2, n=4)
    df = frame(rows)
    kept = list(range(8))   # days 0 and 1 fully clean, day 2 none
    assert calculator.count_good_days(df, kept) == int(calculator.day_quality(df, kept)["good"].sum()) == 2


def test_day_quality_empty_when_no_production():
    dq = calculator.day_quality(frame([{TS: at(0, 3), MK: 0.0}]), [])
    assert list(dq.columns) == ["period", "n_prod", "n_clean", "frac", "good"]
    assert dq.empty
    assert calculator.count_good_days(frame([{TS: at(0, 3), MK: 0.0}]), []) == 0
