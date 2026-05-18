"""Tests for the constraint-checking module."""
from app.research import constraints as c


_RISK = {"constraints": {
    "max_single_position_pct": 25,
    "max_sector_pct": 45,
    "min_cash_buffer_pct": 5,
}}


def _exposures(positions, cash_pct=10, sector_pct=None):
    return {
        "portfolio_value": 10000,
        "cash_pct": cash_pct,
        "sector_pct": sector_pct or {},
        "positions": positions,
        "unpriced_count": 0,
    }


def test_no_breaches_returns_empty():
    out = c.check_constraints(_exposures([
        {"ticker": "A", "weight_pct": 10, "price": 100, "shares": 1},
        {"ticker": "B", "weight_pct": 10, "price": 100, "shares": 1},
    ]), _RISK)
    assert out == []


def test_single_position_breach():
    out = c.check_constraints(_exposures([
        {"ticker": "META", "weight_pct": 64, "price": 600, "shares": 11},
    ]), _RISK)
    assert len(out) == 1
    b = out[0]
    assert b["type"] == "single_position"
    assert b["severity"] == "breach"
    assert b["subject"] == "META"
    assert b["current_pct"] == 64
    assert b["limit_pct"] == 25
    assert "trim" in b["suggested_action"].lower()
    assert "META" in b["suggested_action"]


def test_single_position_warn_at_80_percent_of_limit():
    out = c.check_constraints(_exposures([
        {"ticker": "X", "weight_pct": 22, "price": 100, "shares": 10},
    ]), _RISK)
    assert len(out) == 1
    assert out[0]["severity"] == "warn"


def test_sector_breach():
    out = c.check_constraints(_exposures(
        positions=[],
        sector_pct={"Technology": 60, "Energy": 10},
    ), _RISK)
    types = {b["subject"]: b["severity"] for b in out}
    assert types.get("Technology") == "breach"


def test_low_cash_breach():
    out = c.check_constraints(_exposures([], cash_pct=2), _RISK)
    cash = [b for b in out if b["type"] == "cash"]
    assert cash and cash[0]["severity"] == "breach"


def test_unpriced_positions_surface_as_data_warn():
    exp = _exposures([])
    exp["unpriced_count"] = 3
    out = c.check_constraints(exp, _RISK)
    data = [b for b in out if b["type"] == "data"]
    assert data and "3 position" in data[0]["suggested_action"]


def test_breaches_sorted_severity_first():
    out = c.check_constraints(_exposures([
        {"ticker": "BIG", "weight_pct": 60, "price": 100, "shares": 6},
        {"ticker": "WARN", "weight_pct": 22, "price": 100, "shares": 22},
    ]), _RISK)
    assert out[0]["severity"] == "breach"
    assert out[1]["severity"] == "warn"


def test_none_weight_skipped():
    out = c.check_constraints(_exposures([
        {"ticker": "UNPRICED", "weight_pct": None, "price": None, "shares": 5},
    ]), _RISK)
    assert out == []
