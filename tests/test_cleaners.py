"""Individual filter behaviours and the adaptive per-site phase-threshold search."""
import numpy as np
import pytest

from src import config, cleaners
from helpers import TS, MK, inv_col, at, producing_day, frame


# ── individual filters ───────────────────────────────────────────────────────────

def test_filter_inverter_active_drops_nonpositive_and_null():
    df = frame([
        {TS: at(0, 12, 0),  MK: 90.0, inv_col(1): 50.0,   inv_col(2): 40.0},    # keep
        {TS: at(0, 12, 15), MK: 90.0, inv_col(1): 0.0,    inv_col(2): 40.0},    # drop (zero)
        {TS: at(0, 12, 30), MK: 90.0, inv_col(1): 50.0,   inv_col(2): np.nan},  # drop (null)
        {TS: at(0, 12, 45), MK: 90.0, inv_col(1): -1.0,   inv_col(2): 40.0},    # drop (negative)
    ])
    assert list(cleaners.filter_inverter_active(df, "t").index) == [0]


def test_filter_value_spikes_drops_only_extreme_rows():
    df = frame([
        {TS: at(0, 12, 0),  MK: 90.0, inv_col(1): 50.0},      # 50 kW -> 12.5 kWh, keep
        {TS: at(0, 12, 15), MK: 90.0, inv_col(1): 60000.0},   # 60000 kW -> 15000 kWh > limit, drop
    ])
    assert list(cleaners.filter_value_spikes(df, "t").index) == [0]


def test_filter_gross_outliers_keeps_band_and_nan():
    df = frame([
        {TS: at(0, 12, 0),  MK: 90.0,  inv_col(1): 100.0},   # eff 90  -> keep
        {TS: at(0, 12, 15), MK: 200.0, inv_col(1): 100.0},   # eff 200 -> drop (>150)
        {TS: at(0, 12, 30), MK: 30.0,  inv_col(1): 100.0},   # eff 30  -> drop (<50)
        {TS: at(0, 12, 45), MK: 90.0,  inv_col(1): 0.0},     # inv 0 -> eff NaN -> keep
    ])
    assert set(cleaners.filter_gross_outliers(df, "t").index) == {0, 3}


def test_filter_phase_current_drops_imbalanced_keeps_balanced():
    df = frame([
        {TS: at(0, 12, 0),  MK: 90.0, "PS1 IacA": 100.0, "PS1 IacB": 100.0, "PS1 IacC": 100.0},  # keep
        {TS: at(0, 12, 15), MK: 90.0, "PS1 IacA": 100.0, "PS1 IacB": 100.0, "PS1 IacC": 50.0},   # drop
    ])
    assert list(cleaners.filter_phase_current(df, "t").index) == [0]


def test_filter_phase_current_skips_below_generation_floor():
    df = frame([{TS: at(0, 3), MK: 0.5, "PS1 IacA": 100.0, "PS1 IacB": 100.0, "PS1 IacC": 1.0}])
    assert list(cleaners.filter_phase_current(df, "t").index) == [0]   # below floor -> untouched


def test_filter_phase_current_daytime_scoping_excludes_nonreporting():
    df = frame([{TS: at(0, 12), MK: 90.0, "Inverter 01": np.nan,
                 "PS1 IacA": 100.0, "PS1 IacB": 100.0, "PS1 IacC": 50.0}])
    assert cleaners.filter_phase_current(df, "t", daytime_only=False).empty          # flagged
    assert list(cleaners.filter_phase_current(df, "t", daytime_only=True).index) == [0]  # not reporting -> kept


# ── adaptive per-site phase threshold (run_all_filters) ─────────────────────────

def _phase_site(n_days, legs, inverters=(50.0, 50.0), n=4):
    rows = []
    for d in range(n_days):
        rows += producing_day(d, n=n, meter=90.0, inverters=inverters, legs=legs)
    return frame(rows)


def test_run_all_filters_records_search_metadata():
    out = cleaners.run_all_filters(_phase_site(3, legs=(100.0, 100.0, 100.0)), "t")
    for key in ("phase_threshold_used", "phase_threshold_iterations", "phase_threshold_hit_ceiling",
                "phase_threshold_good_days", "phase_threshold_ceiling"):
        assert key in out.attrs


def test_run_all_filters_no_adjust_when_floor_met_at_start():
    out = cleaners.run_all_filters(_phase_site(25, legs=(100.0, 100.0, 100.0)), "t")
    assert out.attrs["phase_threshold_iterations"] == 1
    assert out.attrs["phase_threshold_used"] == pytest.approx(config.MAX_PHASE_CURRENT_DEVIATION)
    assert out.attrs["phase_threshold_good_days"] >= config.MIN_GOOD_DAYS_ADAPTIVE


def test_run_all_filters_steps_up_until_floor_cleared():
    # 25 days at 6.5% leg imbalance: phase fails at 0.04/0.05/0.06, passes at 0.07.
    out = cleaners.run_all_filters(_phase_site(25, legs=(106.5, 100.0, 93.5)), "t")
    assert out.attrs["phase_threshold_used"] == pytest.approx(0.07)
    assert out.attrs["phase_threshold_iterations"] == 4
    assert out.attrs["phase_threshold_hit_ceiling"] is False
    assert out.attrs["phase_threshold_good_days"] >= config.MIN_GOOD_DAYS_ADAPTIVE


def test_run_all_filters_stops_at_ceiling_when_unreachable():
    # 40% imbalance never clears the phase check -> climbs to the ceiling, stays below the floor.
    out = cleaners.run_all_filters(_phase_site(25, legs=(140.0, 100.0, 60.0)), "t")
    assert out.attrs["phase_threshold_used"] == pytest.approx(config.MAX_PHASE_CURRENT_DEVIATION_CEILING)
    assert out.attrs["phase_threshold_hit_ceiling"] is True
    assert out.attrs["phase_threshold_good_days"] < config.MIN_GOOD_DAYS_ADAPTIVE
