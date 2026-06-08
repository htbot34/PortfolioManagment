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


# ---------------------------------------------------------------------------
# vol_ratio_20d must exclude a still-forming current-session bar (fix #1):
# an intraday build divides a partial day's cumulative volume by a full-day
# average, yielding a structurally tiny ratio that starves the offense path.
# ---------------------------------------------------------------------------
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def test_session_in_progress_logic():
    d = date(2026, 6, 5)
    assert prices._session_in_progress(d, datetime.combine(d, time(9, 58), tzinfo=_ET)) is True
    # At/after the 16:00 close the session is no longer in progress.
    assert prices._session_in_progress(d, datetime.combine(d, time(16, 0), tzinfo=_ET)) is False
    assert prices._session_in_progress(d, datetime.combine(d, time(17, 30), tzinfo=_ET)) is False
    # A prior-day bar is always complete.
    nxt = datetime.combine(d + timedelta(days=1), time(9, 0), tzinfo=_ET)
    assert prices._session_in_progress(d, nxt) is False
    # Missing inputs are safe.
    assert prices._session_in_progress(None, nxt) is False
    assert prices._session_in_progress(d, None) is False


def test_volume_ratio_excludes_partial_current_session():
    idx = pd.date_range("2026-05-01", periods=25, freq="B")
    # 24 full days at 1000, plus a partial 'today' bar at 120.
    vol = pd.Series([1000.0] * 24 + [120.0], index=idx)
    last_date = idx[-1].date()
    intraday = datetime.combine(last_date, time(10, 0), tzinfo=_ET)
    # During the session the partial bar is dropped -> 1000 / 1000 = 1.0.
    assert prices._volume_ratio(vol, last_date, intraday) == pytest.approx(1.0)
    # The buggy (no-exclusion) value would have been ~0.12 -- the starvation.
    assert 120.0 / float(vol.tail(20).mean()) < 0.2


def test_volume_ratio_keeps_bar_after_close():
    idx = pd.date_range("2026-05-01", periods=25, freq="B")
    vol = pd.Series([1000.0] * 24 + [3000.0], index=idx)  # last bar complete (high)
    last_date = idx[-1].date()
    after = datetime.combine(last_date, time(16, 30), tzinfo=_ET)
    naive = 3000.0 / float(vol.tail(20).mean())
    assert prices._volume_ratio(vol, last_date, after) == pytest.approx(naive)


def test_volume_ratio_prior_day_bar_unchanged():
    idx = pd.date_range("2026-05-01", periods=25, freq="B")
    vol = pd.Series([1000.0] * 24 + [3000.0], index=idx)
    last_date = idx[-1].date()
    tomorrow = datetime.combine(last_date + timedelta(days=1), time(9, 0), tzinfo=_ET)
    naive = 3000.0 / float(vol.tail(20).mean())
    assert prices._volume_ratio(vol, last_date, tomorrow) == pytest.approx(naive)


def test_volume_ratio_none_when_no_volume():
    assert prices._volume_ratio(None, date(2026, 6, 5), None) is None
