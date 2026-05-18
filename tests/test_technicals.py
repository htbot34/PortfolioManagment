"""Tests for Wilder RSI + SMA series + cross detection."""
import pandas as pd
import pytest

from app.data import prices


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx)


def test_wilder_rsi_classic_series():
    """Standard test case used in textbooks for Wilder smoothing.

    For a perfectly trending up series the RSI should approach 100; for a
    flat-then-down series it should fall below 30.
    """
    up = _series(list(range(1, 31)))
    rsi = prices._wilder_rsi(up, 14).dropna()
    assert rsi.iloc[-1] > 95

    down = _series([100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89, 88, 87, 86, 85, 84, 83, 82, 81])
    rsi_d = prices._wilder_rsi(down, 14).dropna()
    assert rsi_d.iloc[-1] < 5


def test_wilder_rsi_short_series_returns_nones():
    s = _series([1, 2, 3, 4, 5])
    out = prices._wilder_rsi(s, 14)
    assert out.isna().all()


def test_series_dump_shape():
    s = _series([1.0, 2.0, 3.0])
    out = prices._series_dump(s, tail=10)
    assert out == [
        {"date": "2025-01-01", "value": 1.0},
        {"date": "2025-01-02", "value": 2.0},
        {"date": "2025-01-03", "value": 3.0},
    ]
