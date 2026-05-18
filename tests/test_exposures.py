"""Tests for compute_exposures concentration math when prices fail."""
from dataclasses import dataclass
from unittest.mock import patch

from app.portfolio import store
from app.research import portfolio_review


class _Quote:
    def __init__(self, price, sector="Tech"):
        self.price = price
        self.day_change_pct = 1.0 if price else None
        self.sector = sector


def test_unpriced_position_flagged_and_excluded_from_weights():
    acct = store.Account(
        cash=1000.0, total_value=0, currency="USD",
        positions=[
            store.Position("AAPL", 10, 100.0),
            store.Position("BADTICK", 5, 200.0),
            store.Position("MSFT", 4, 250.0),
        ],
    )
    qs = {
        "AAPL": _Quote(150.0, "Tech"),
        "BADTICK": _Quote(None, None),
        "MSFT": _Quote(300.0, "Tech"),
    }
    with patch.object(portfolio_review.prices, "quotes", return_value=qs):
        out = portfolio_review.compute_exposures(acct)

    rows = {r["ticker"]: r for r in out["positions"]}
    assert rows["AAPL"]["price_unavailable"] is False
    assert rows["BADTICK"]["price_unavailable"] is True
    assert rows["BADTICK"]["weight_pct"] is None
    assert rows["BADTICK"]["market_value"] is None
    assert out["unpriced_count"] == 1
    assert out["priced_count"] == 2

    # Weights of priced positions should sum to 100% of (priced_mv + cash).
    priced_mv = 10 * 150.0 + 4 * 300.0
    total = priced_mv + 1000.0
    expected_aapl = (10 * 150.0) / total * 100
    expected_msft = (4 * 300.0) / total * 100
    assert abs(rows["AAPL"]["weight_pct"] - expected_aapl) < 1e-6
    assert abs(rows["MSFT"]["weight_pct"] - expected_msft) < 1e-6
