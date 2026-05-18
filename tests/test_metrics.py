"""Tests for portfolio risk + benchmark metrics."""
import math

import numpy as np
import pandas as pd
import pytest

from app.research import metrics


def _flat(value: float, n: int = 252) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.Series([value] * n, index=idx)


def test_annualize_return_flat_zero():
    assert metrics._annualize_return(_flat(0.0)) == 0.0


def test_annualize_return_positive_constant():
    # 0.1% daily for 252 days -> roughly (1.001)^252 - 1 ~= 28.7%
    out = metrics._annualize_return(_flat(0.001))
    assert 0.27 < out < 0.30


def test_annualize_vol_zero_for_flat():
    assert metrics._annualize_vol(_flat(0.001)) < 1e-9


def test_max_drawdown_positive_only_is_zero():
    s = _flat(0.001)
    assert metrics._max_drawdown(s) == 0.0


def test_max_drawdown_detects_decline():
    # +1% for 100 days, then -2% for 50 days
    rets = pd.Series([0.01] * 100 + [-0.02] * 50,
                      index=pd.date_range("2025-01-01", periods=150, freq="B"))
    dd = metrics._max_drawdown(rets)
    # Peak cumprod ~ (1.01)^100 ~ 2.70; trough ~ 2.70 * (0.98)^50 ~ 0.992
    # so drawdown ~ -63%
    assert dd < -0.5


def test_sharpe_positive_for_positive_excess():
    np.random.seed(0)
    rets = pd.Series(np.random.normal(0.0005, 0.01, 500),
                      index=pd.date_range("2024-01-01", periods=500, freq="B"))
    s = metrics._sharpe(rets, rf=0.0)
    assert s > 0


def test_sortino_higher_than_sharpe_when_downside_is_small():
    # Series with mostly small positive returns and rare large up days.
    np.random.seed(1)
    base = np.random.normal(0.001, 0.005, 500)
    base[::20] += 0.05  # occasional spike up - no extra downside
    rets = pd.Series(base, index=pd.date_range("2024-01-01", periods=500, freq="B"))
    sortino = metrics._sortino(rets, rf=0.0)
    sharpe = metrics._sharpe(rets, rf=0.0)
    assert sortino > sharpe


def test_beta_alpha_basic_relation():
    # Portfolio = 2x benchmark exactly -> beta ~ 2, alpha ~ 0
    np.random.seed(2)
    bench = pd.Series(np.random.normal(0.0005, 0.01, 500),
                      index=pd.date_range("2024-01-01", periods=500, freq="B"))
    port = bench * 2
    beta, alpha = metrics._beta_alpha(port, bench, rf=0.0)
    assert abs(beta - 2.0) < 0.05
    assert abs(alpha) < 0.02


def test_sparkline_downsamples():
    s = _flat(0.001, n=400)
    pts = metrics._sparkline(s, points=60)
    assert 50 <= len(pts) <= 100  # roughly 60 with floor/ceiling


def test_sparkline_empty_returns_empty_list():
    assert metrics._sparkline(pd.Series(dtype=float)) == []


def test_total_return_compounds():
    rets = pd.Series([0.10, 0.10],
                      index=pd.date_range("2025-01-01", periods=2, freq="B"))
    assert abs(metrics._total_return(rets) - 0.21) < 1e-9
