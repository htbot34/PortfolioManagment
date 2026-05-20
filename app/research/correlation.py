"""Portfolio correlation analysis.

Before promoting a new-position rec we check whether the candidate just
piles more factor risk onto the existing book. Two free, deterministic
computations over daily returns (90-day window by default):

- ``compute_position_correlations`` -- symmetric correlation matrix across
  the current holdings.
- ``candidate_correlation_to_book`` -- a candidate ticker's correlation to
  the book, focused on the 5 largest positions by market value.

``prices_provider`` is any callable ``(ticker) -> DataFrame | None`` with a
``Close`` column; it defaults to ``app.data.prices.history`` whose multi-source
chain already caches in-process, so repeated calls inside one build are free.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from app.logging import get_logger

log = get_logger(__name__)

_MIN_OVERLAP = 20  # daily-return points needed for a usable correlation


def _default_provider(ticker: str):
    from app.data import prices
    return prices.history(ticker)


def _history_df(ticker: str, provider: Callable):
    try:
        df = provider(ticker)
    except Exception as e:
        log.debug("correlation: provider failed for %s: %s", ticker, e)
        return None
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return None
    return df


def _daily_returns(df, window_days: int) -> pd.Series | None:
    close = df["Close"].astype(float)
    rets = close.pct_change().dropna()
    if window_days:
        rets = rets.tail(window_days)
    return rets if len(rets) >= _MIN_OVERLAP else None


def _pair_corr(a: pd.Series, b: pd.Series) -> float | None:
    aligned = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(aligned) < _MIN_OVERLAP:
        return None
    c = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
    if c is None or pd.isna(c):
        return None
    return round(float(c), 4)


def compute_position_correlations(
    portfolio, prices_provider: Callable | None = None, window_days: int = 90,
) -> dict:
    """Return a symmetric correlation matrix of daily returns across holdings.

    Result::

        {"matrix": {"AAPL": {"AAPL": 1.0, "MSFT": 0.62, ...}, ...},
         "tickers": [...],          # tickers actually in the matrix
         "missing": [...]}          # holdings with no usable price history
    """
    provider = prices_provider or _default_provider
    tickers = [p.ticker.upper() for p in getattr(portfolio, "positions", [])]
    returns: dict[str, pd.Series] = {}
    for t in tickers:
        df = _history_df(t, provider)
        if df is None:
            continue
        r = _daily_returns(df, window_days)
        if r is not None:
            returns[t] = r
    missing = [t for t in tickers if t not in returns]
    if len(returns) < 2:
        return {"matrix": {}, "tickers": list(returns.keys()), "missing": missing}
    frame = pd.DataFrame(returns).dropna()
    corr = frame.corr()
    matrix = {
        a: {b: round(float(corr.loc[a, b]), 4) for b in corr.columns}
        for a in corr.index
    }
    return {"matrix": matrix, "tickers": list(corr.columns), "missing": missing}


def candidate_correlation_to_book(
    candidate_ticker: str, portfolio,
    prices_provider: Callable | None = None, window_days: int = 90,
) -> dict:
    """Correlation of ``candidate_ticker``'s daily returns to the book.

    Result::

        {"available": bool,
         "avg_corr_to_top5": float | None,   # mean corr to 5 largest positions
         "max_corr": float | None,
         "max_corr_ticker": str | None,
         "highly_correlated_holdings": [tickers with corr > 0.7, desc],
         "all_correlations": {ticker: corr},
         "top5": [tickers]}

    "Top 5" = the 5 largest positions by market value (shares * latest close).
    """
    provider = prices_provider or _default_provider
    candidate_ticker = candidate_ticker.upper()
    none_out = {
        "available": False, "avg_corr_to_top5": None, "max_corr": None,
        "max_corr_ticker": None, "highly_correlated_holdings": [],
        "all_correlations": {}, "top5": [],
    }

    cand_df = _history_df(candidate_ticker, provider)
    if cand_df is None:
        return none_out
    cand_rets = _daily_returns(cand_df, window_days)
    if cand_rets is None:
        return none_out

    holdings = [p for p in getattr(portfolio, "positions", [])
                if p.ticker.upper() != candidate_ticker]
    if not holdings:
        return none_out

    market_value: dict[str, float] = {}
    corrs: dict[str, float] = {}
    for p in holdings:
        t = p.ticker.upper()
        df = _history_df(t, provider)
        if df is None:
            continue
        rets = _daily_returns(df, window_days)
        if rets is None:
            continue
        c = _pair_corr(cand_rets, rets)
        if c is None:
            continue
        corrs[t] = c
        last_close = float(df["Close"].iloc[-1])
        market_value[t] = last_close * p.shares

    if not corrs:
        return none_out

    top5 = sorted(market_value, key=lambda k: market_value[k], reverse=True)[:5]
    top5_corrs = [corrs[t] for t in top5 if t in corrs]
    avg_top5 = round(sum(top5_corrs) / len(top5_corrs), 4) if top5_corrs else None
    max_ticker = max(corrs, key=lambda k: corrs[k])
    highly = sorted((t for t, c in corrs.items() if c > 0.7),
                    key=lambda k: corrs[k], reverse=True)
    return {
        "available": True,
        "avg_corr_to_top5": avg_top5,
        "max_corr": corrs[max_ticker],
        "max_corr_ticker": max_ticker,
        "highly_correlated_holdings": highly,
        "all_correlations": corrs,
        "top5": top5,
    }
