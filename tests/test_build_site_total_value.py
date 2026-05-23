"""Auto-recompute account.total_value from live market values + cash."""
import pytest

from app import build_site
from app.data.prices import Quote
from app.portfolio import store
from app.research import portfolio_review


def _stub_quote(price: float):
    def _quote(ticker, fast=False):
        return Quote(
            ticker=ticker, price=price, prev_close=None, day_change_pct=None,
            market_cap=None, pe_ratio=None, high_52w=None, low_52w=None,
            sector=None, industry=None,
        )
    return _quote


def _write_portfolio(path, cash, total_value, positions):
    body = ["account:", f"  cash: {cash}", f"  total_value: {total_value}",
            "  currency: USD", "positions:"]
    if not positions:
        body[-1] = "positions: []"
    else:
        for t, shares, cb in positions:
            body.extend([f"  - ticker: {t}",
                         f"    shares: {shares}",
                         f"    cost_basis: {cb}"])
    path.write_text("\n".join(body) + "\n")


def test_recompute_total_from_positions_and_cash(tmp_path, monkeypatch):
    p = tmp_path / "portfolio.yaml"
    _write_portfolio(p, cash=100.0, total_value=0.0,
                     positions=[("AAA", 10, 5.0)])
    monkeypatch.setattr(portfolio_review.prices, "quote", _stub_quote(15.0))
    account = store.load(p)
    exposures = portfolio_review.compute_exposures(account)
    new_total, changed = build_site.recompute_total_value(account, exposures,
                                                           path=p)
    # 10 shares * $15 + $100 cash = $250
    assert new_total == 250.0
    assert changed is True
    assert store.load(p).total_value == 250.0


def test_recompute_total_for_cash_only_account(tmp_path, monkeypatch):
    p = tmp_path / "portfolio.yaml"
    _write_portfolio(p, cash=500.0, total_value=0.0, positions=[])
    # No positions -> no quote calls, but stub anyway in case
    monkeypatch.setattr(portfolio_review.prices, "quote", _stub_quote(0.0))
    account = store.load(p)
    exposures = portfolio_review.compute_exposures(account)
    new_total, changed = build_site.recompute_total_value(account, exposures,
                                                           path=p)
    assert new_total == 500.0
    assert changed is True
    assert store.load(p).total_value == 500.0


def test_recompute_total_is_idempotent(tmp_path, monkeypatch):
    p = tmp_path / "portfolio.yaml"
    _write_portfolio(p, cash=100.0, total_value=250.0,
                     positions=[("AAA", 10, 5.0)])
    monkeypatch.setattr(portfolio_review.prices, "quote", _stub_quote(15.0))
    account = store.load(p)
    exposures = portfolio_review.compute_exposures(account)
    new_total, changed = build_site.recompute_total_value(account, exposures,
                                                           path=p)
    assert new_total == 250.0
    assert changed is False   # already correct -> no write
