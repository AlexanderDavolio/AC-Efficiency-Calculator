"""Cumulative-register detection and conversion (src/cumulative.py)."""
import numpy as np
import pandas as pd
import pytest

from src import cumulative
from helpers import at


def _ts(n):
    return pd.Series([at(0, 0, 15 * i) for i in range(n)])


def test_cumulative_register_is_differenced():
    n = 50
    cum = pd.Series(np.arange(n, dtype=float) * 10.0)   # 0,10,20,... strictly increasing
    out = cumulative.to_interval_if_cumulative(cum, _ts(n), "Meter kWh", "t")
    assert np.isnan(out.iloc[0])                          # first interval has no predecessor
    assert out.iloc[1:].eq(10.0).all()                    # every step differences to +10


def test_per_interval_series_left_unchanged():
    n = 50
    shape = np.concatenate([np.arange(25), np.arange(25)[::-1]]).astype(float)  # rises then falls
    s = pd.Series(shape)
    out = cumulative.to_interval_if_cumulative(s, _ts(n), "X", "t")
    pd.testing.assert_series_equal(out, s)


def test_rollover_is_clamped_to_zero():
    s = pd.Series(np.concatenate([np.arange(0, 30, 1.0), np.arange(0, 20, 1.0)]))  # reset at idx 30
    out = cumulative.to_interval_if_cumulative(s, _ts(50), "Meter", "t")
    assert (out.dropna() >= 0).all()    # the -29 reset step must not appear as negative energy
    assert out.iloc[30] == 0.0


def test_too_few_rows_assumed_per_interval():
    s = pd.Series(np.arange(10, dtype=float) * 10.0)     # looks cumulative but < CUMULATIVE_MIN_ROWS
    out = cumulative.to_interval_if_cumulative(s, _ts(10), "X", "t")
    pd.testing.assert_series_equal(out, s)
