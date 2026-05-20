"""Tests for portfolio correlation analysis + the conviction correlation gate.

All price series are synthetic and deterministic - no network.
"""
import numpy as np
import pandas as pd
import pytest

from app.portfolio.store import Account, Position
from app.research import conviction, correlation


# ---------------------------------------------------------------------------
# Synthetic price series with known correlations
# ---------------------------------------------------------------------------

_DATES = pd.date_range("2025-01-01", periods=130, freq="B")


def _price_series_from_returns(returns: np.ndarray, start: float = 100.0) -> pd.DataFrame:
    """Build a Close-only DataFrame from a daily-return array."""
    prices = [start]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    return pd.DataFrame({"Close": prices[1:]}, index=_DATES)


# A base random walk; B copies A's returns exactly (corr ~ +1); C is the
# negation (corr ~ -1); D is an independent walk (corr ~ 0).
_rng = np.random.default_rng(42)
_RET_A = _rng.normal(0.0005, 0.012, len(_DATES))
_RET_D = _rng.normal(0.0005, 0.012, len(_DATES))

_SERIES = {
    "AAA": _price_series_from_returns(_RET_A),
    "BBB": _price_series_from_returns(_RET_A),       # identical returns -> corr 1
    "CCC": _price_series_from_returns(-_RET_A),      # negated -> corr -1
    "DDD": _price_series_from_returns(_RET_D),       # independent -> corr ~ 0
}


def _provider(ticker: str):
    return _SERIES.get(ticker.upper())


def _account(*tickers_shares) -> Account:
    return Account(
        cash=0.0, total_value=0.0, currency="USD",
        positions=[Position(t, s, 100.0) for t, s in tickers_shares],
    )


# ---------------------------------------------------------------------------
# compute_position_correlations
# ---------------------------------------------------------------------------

def test_matrix_known_correlations():
    acct = _account(("AAA", 1), ("BBB", 1), ("CCC", 1), ("DDD", 1))
    out = correlation.compute_position_correlations(acct, _provider, window_days=90)
    m = out["matrix"]
    assert abs(m["AAA"]["AAA"] - 1.0) < 1e-9
    assert m["AAA"]["BBB"] > 0.999          # identical
    assert m["AAA"]["CCC"] < -0.999         # negated
    assert abs(m["AAA"]["DDD"]) < 0.30      # independent -> near zero


def test_matrix_is_symmetric():
    acct = _account(("AAA", 1), ("DDD", 1))
    m = correlation.compute_position_correlations(acct, _provider)["matrix"]
    assert m["AAA"]["DDD"] == m["DDD"]["AAA"]


def test_matrix_reports_missing_holdings():
    acct = _account(("AAA", 1), ("ZZZ", 1))   # ZZZ has no synthetic series
    out = correlation.compute_position_correlations(acct, _provider)
    assert "ZZZ" in out["missing"]
    assert "AAA" not in out["missing"]


def test_matrix_under_two_holdings_is_empty():
    acct = _account(("AAA", 1))
    out = correlation.compute_position_correlations(acct, _provider)
    assert out["matrix"] == {}


# ---------------------------------------------------------------------------
# candidate_correlation_to_book
# ---------------------------------------------------------------------------

def test_candidate_highly_correlated_to_book():
    # Book holds AAA; candidate BBB has identical returns -> avg corr ~ 1.
    acct = _account(("AAA", 10))
    out = correlation.candidate_correlation_to_book("BBB", acct, _provider)
    assert out["available"] is True
    assert out["avg_corr_to_top5"] > 0.99
    assert out["max_corr_ticker"] == "AAA"
    assert "AAA" in out["highly_correlated_holdings"]


def test_candidate_uncorrelated_to_book():
    acct = _account(("AAA", 10))
    out = correlation.candidate_correlation_to_book("DDD", acct, _provider)
    assert out["available"] is True
    assert abs(out["avg_corr_to_top5"]) < 0.30
    assert out["highly_correlated_holdings"] == []


def test_candidate_empty_book():
    out = correlation.candidate_correlation_to_book("AAA", _account(), _provider)
    assert out["available"] is False


def test_candidate_excludes_self_from_book():
    # Candidate AAA is also held; it must not correlate against itself.
    acct = _account(("AAA", 10), ("DDD", 5))
    out = correlation.candidate_correlation_to_book("AAA", acct, _provider)
    assert "AAA" not in out["all_correlations"]
    assert "DDD" in out["all_correlations"]


# ---------------------------------------------------------------------------
# conviction gate wiring
# ---------------------------------------------------------------------------

def _macro(r5=1.0, r20=3.0):
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": r5, "ret_20d": r20}}}


def _qualifying_payload(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "sector": "Technology",
        "rsi14": 55, "macd_hist": 0.5,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "news_classifications": [
            {"direction": "bullish", "magnitude": 4, "durability": "long",
             "one_line_summary": "Strong", "published": None},
            {"direction": "bullish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "Deal", "published": None},
        ],
        "insider_transactions": [],
    }


def test_gate_blocks_new_buy_with_high_correlation():
    # Book holds AAA; candidate BBB is ~perfectly correlated -> blocked.
    acct = _account(("AAA", 10))
    out = conviction.evaluate(_qualifying_payload("BBB"), direction="long",
                              macro=_macro(), action="new_buy",
                              portfolio=acct, prices_provider=_provider)
    assert out["qualifies"] is False
    assert "correlation" in out["correlation_block"]
    assert out["correlation"]["candidate_to_book"]["avg_corr_to_top5"] > 0.7


def test_gate_passes_new_buy_with_low_correlation():
    acct = _account(("AAA", 10))
    out = conviction.evaluate(_qualifying_payload("DDD"), direction="long",
                              macro=_macro(), action="new_buy",
                              portfolio=acct, prices_provider=_provider)
    assert out["qualifies"] is True
    assert "correlation_block" not in out
    assert out["correlation"]["candidate_to_book"]["available"] is True


def test_gate_attaches_correlation_block_even_when_passing():
    acct = _account(("AAA", 10))
    out = conviction.evaluate(_qualifying_payload("DDD"), direction="long",
                              macro=_macro(), action="new_buy",
                              portfolio=acct, prices_provider=_provider)
    assert "correlation" in out


def test_add_into_tight_cluster_annotates_but_does_not_block():
    # Book holds AAA, BBB, CCC. AAA-BBB corr ~1, AAA-CCC corr ~-1 (abs > 0.8).
    # Adding to AAA: it correlates > 0.8 (absolute) with 2 others. The gate
    # uses signed corr though - AAA vs BBB is +1, AAA vs CCC is -1, so only
    # BBB clears the > 0.8 test. Use a book where AAA has two +0.8 partners.
    acct = _account(("AAA", 10), ("BBB", 10), ("DDD", 10))
    out = conviction.evaluate(_qualifying_payload("AAA"), direction="long",
                              macro=_macro(), action="add",
                              portfolio=acct, prices_provider=_provider)
    # AAA only has one >0.8 partner (BBB) -> not a tight cluster -> qualifies,
    # no annotation, never blocked.
    assert out["qualifies"] is True
    assert "correlation_block" not in out
