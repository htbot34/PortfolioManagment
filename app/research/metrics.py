"""Portfolio risk + benchmark metrics.

Computes daily portfolio returns using *current* weights as a proxy (a
simplification that ignores historical rebalancing - documented below), then
compares them against a benchmark (SPY by default) for the standard set of
analyst metrics: annualized return, annualized volatility, Sharpe, Sortino,
max drawdown, beta, alpha, and the period's total return delta.

Static-site context: no FastAPI endpoint - the build script calls
``compute_metrics(account)`` and feeds the result into the positions.html
template.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from app.data import prices
from app.logging import get_logger
from app.portfolio.store import Account

log = get_logger(__name__)

_TRADING_DAYS = 252
_DEFAULT_RF = 0.04  # annualized risk-free; override via parameter


def _aligned_returns(
    tickers: list[str], period: str = "1y"
) -> tuple[pd.DataFrame, list[str]]:
    """Return a DataFrame of daily simple returns indexed by date, columns=tickers
    that actually returned data. The second list is tickers we couldn't price.
    """
    frames: dict[str, pd.Series] = {}
    missing: list[str] = []
    for t in tickers:
        df = prices.history(t, period=period)
        if df is None or df.empty or "Close" not in df.columns:
            missing.append(t)
            continue
        s = df["Close"].astype(float).pct_change().dropna()
        if s.empty:
            missing.append(t)
            continue
        frames[t.upper()] = s
    if not frames:
        return pd.DataFrame(), missing
    df = pd.concat(frames, axis=1).dropna(how="all")
    df = df.fillna(0.0)  # tickers that didn't trade on a given day -> 0% move
    return df, missing


def portfolio_returns(account: Account, period: str = "1y") -> pd.Series:
    """Daily portfolio simple-return series using current weights as a proxy.

    Simplification: applies today's market-value weights to each historical
    day. This ignores actual trading history. The result is still useful for
    risk/benchmark comparison because today's exposures dominate the look of
    forward-looking risk metrics anyway, but it should not be confused with
    a backtested track record.
    """
    if not account.positions:
        return pd.Series(dtype=float)
    tickers = [p.ticker for p in account.positions]
    rets, missing = _aligned_returns(tickers, period=period)
    if rets.empty:
        return pd.Series(dtype=float)
    # Weight each priced position by its current market value.
    weights: dict[str, float] = {}
    total_mv = 0.0
    for p in account.positions:
        q = prices.quote(p.ticker)
        if q.price is None:
            continue
        mv = q.price * p.shares
        weights[p.ticker.upper()] = mv
        total_mv += mv
    if total_mv <= 0:
        return pd.Series(dtype=float)
    w = pd.Series({t: weights[t] / total_mv for t in rets.columns if t in weights})
    if w.empty:
        return pd.Series(dtype=float)
    portfolio = rets[w.index].mul(w, axis=1).sum(axis=1)
    return portfolio


def _benchmark_returns(benchmark: str, period: str = "1y") -> pd.Series:
    df = prices.history(benchmark, period=period)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    return df["Close"].astype(float).pct_change().dropna()


def _max_drawdown(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    cum = (1 + series).cumprod()
    peak = cum.cummax()
    dd = (cum / peak) - 1
    return float(dd.min())


def _annualize_return(daily: pd.Series) -> float:
    if daily.empty:
        return 0.0
    return float((1 + daily.mean()) ** _TRADING_DAYS - 1)


def _annualize_vol(daily: pd.Series) -> float:
    if daily.empty:
        return 0.0
    return float(daily.std(ddof=1) * math.sqrt(_TRADING_DAYS))


def _sharpe(daily: pd.Series, rf: float) -> float:
    if daily.empty:
        return 0.0
    excess = daily - (rf / _TRADING_DAYS)
    s = excess.std(ddof=1)
    if not s:
        return 0.0
    return float((excess.mean() / s) * math.sqrt(_TRADING_DAYS))


def _sortino(daily: pd.Series, rf: float) -> float:
    if daily.empty:
        return 0.0
    excess = daily - (rf / _TRADING_DAYS)
    downside = excess[excess < 0]
    d = downside.std(ddof=1) if not downside.empty else 0.0
    if not d:
        return 0.0
    return float((excess.mean() / d) * math.sqrt(_TRADING_DAYS))


def _beta_alpha(port: pd.Series, bench: pd.Series, rf: float) -> tuple[float, float]:
    aligned = pd.concat([port, bench], axis=1, join="inner").dropna()
    aligned.columns = ["p", "b"]
    if len(aligned) < 30:
        return 0.0, 0.0
    var_b = aligned["b"].var(ddof=1)
    if not var_b:
        return 0.0, 0.0
    cov = aligned["p"].cov(aligned["b"])
    beta = float(cov / var_b)
    ann_p = _annualize_return(aligned["p"])
    ann_b = _annualize_return(aligned["b"])
    alpha = float(ann_p - (rf + beta * (ann_b - rf)))
    return beta, alpha


def _total_return(daily: pd.Series) -> float:
    if daily.empty:
        return 0.0
    return float((1 + daily).prod() - 1)


def _sparkline(daily: pd.Series, points: int = 60) -> list[float]:
    """Return cumulative-return points for an inline-SVG sparkline.

    Down-samples to roughly ``points`` evenly-spaced values so the SVG stays
    small in the generated HTML.
    """
    if daily.empty:
        return []
    cum = (1 + daily).cumprod() - 1  # cumulative total return
    if len(cum) <= points:
        return [float(x) for x in cum.tolist()]
    step = max(1, len(cum) // points)
    sampled = cum.iloc[::step].tolist()
    return [float(x) for x in sampled]


def compute_metrics(
    account: Account, benchmark: str = "SPY", period: str = "1y",
    risk_free: Optional[float] = None,
) -> dict:
    """One-call entry point used by the build script.

    Returns a dict the template can render directly. Keys are documented
    inline. All metrics are annualized where applicable. ``return_pct``
    fields are simple total returns over the period (decimal, e.g. 0.18
    for +18 percent).
    """
    rf = _DEFAULT_RF if risk_free is None else risk_free
    port = portfolio_returns(account, period=period)
    bench = _benchmark_returns(benchmark, period=period)
    if port.empty:
        log.warning("portfolio returns empty - skipping metrics")
        return {"available": False, "reason": "no portfolio return data"}
    aligned = pd.concat([port, bench], axis=1, join="inner").dropna()
    aligned.columns = ["p", "b"]
    port_ret = _total_return(aligned["p"])
    bench_ret = _total_return(aligned["b"]) if not bench.empty else 0.0
    beta, alpha = _beta_alpha(port, bench, rf)
    out = {
        "available": True,
        "benchmark": benchmark,
        "period": period,
        "risk_free": rf,
        "ann_return": _annualize_return(port),
        "ann_vol": _annualize_vol(port),
        "sharpe": _sharpe(port, rf),
        "sortino": _sortino(port, rf),
        "max_drawdown": _max_drawdown(port),
        "beta": beta,
        "alpha": alpha,
        "total_return_pct": port_ret,
        "benchmark_total_return_pct": bench_ret,
        "outperformance_pct": port_ret - bench_ret,
        "sparkline_port": _sparkline(port),
        "sparkline_bench": _sparkline(bench) if not bench.empty else [],
        "n_days": int(len(aligned)),
    }
    return out
