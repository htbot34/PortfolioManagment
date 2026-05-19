"""Tests for app.research.sizing.compute_size.

Verifies the four action types and the constraint-driven downsize path.
"""
from unittest.mock import patch

from app.portfolio.store import Account, Position
from app.research import sizing


def _account(cash: float = 5_000.0, positions=None) -> Account:
    return Account(cash=cash, total_value=0, currency="USD", positions=positions or [])


def _risk(max_single: int = 25, min_cash: int = 5, max_sector: int = 45) -> dict:
    return {"constraints": {
        "max_single_position_pct": max_single,
        "max_sector_pct": max_sector,
        "min_cash_buffer_pct": min_cash,
    }}


def _gate(score: int) -> dict:
    return {"signals": {"technical": {"score": score, "pass": True}}}


# ---------------------------------------------------------------------------
# new_buy
# ---------------------------------------------------------------------------

def test_new_buy_default_targets_3pct():
    acct = _account(cash=5_000)
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("new_buy", "NVDA", current_price=100.0,
                                    position=None, account=acct)
    assert out["rejected"] is False
    # 3% of 5_000 portfolio (just cash) = $150 -> 1 share at $100
    assert out["shares"] == 1
    assert out["dollars"] == 100.0
    assert "1.0% of portfolio" not in out["display"]  # display percent is computed dollars/pv
    assert "Deploy" in out["display"]


def test_new_buy_with_score_3_targets_5pct():
    acct = _account(cash=10_000)
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("new_buy", "NVDA", 100.0, None, acct,
                                    gate=_gate(score=3))
    # 5% of 10_000 = $500 -> 5 shares
    assert out["shares"] == 5
    assert out["dollars"] == 500.0


def test_new_buy_respects_cash_buffer_downsizes():
    # Cash 1_000, min_cash 5% of 10_000 portfolio = $500 buffer -> $500 available.
    acct = _account(cash=1_000, positions=[Position("AAPL", 30, 150)])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("new_buy", "NVDA", 100.0, None, acct,
                                    gate=_gate(score=3))
    # Without downsize, 5% of pv = 5% of 5_500 = $275 < $500 cap, allowed.
    # With score 3, target $275 -> 2 shares = $200.
    assert out["rejected"] is False
    assert out["dollars"] <= 500


def test_new_buy_rejected_when_cash_under_buffer():
    acct = _account(cash=50, positions=[Position("AAPL", 30, 150)])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("new_buy", "NVDA", 100.0, None, acct)
    assert out["rejected"] is True
    assert "buffer" in out["rejection_reason"]


def test_new_buy_rejected_when_price_above_target_dollars():
    acct = _account(cash=100, positions=[])
    # Portfolio_value = 100, 3% = $3, price $1000 -> 0 shares
    with patch.object(sizing, "load_risk_profile", return_value=_risk(min_cash=0)):
        out = sizing.compute_size("new_buy", "NVDA", 1000.0, None, acct)
    assert out["rejected"] is True


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def test_add_raises_weight_by_two_points():
    pos = Position("NVDA", 5, 100)  # 5 shares * $100 = $500
    # Total: cash 5000 + 500 position = 5500. NVDA is 9.1% of pv.
    acct = _account(cash=5_000, positions=[pos])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("add", "NVDA", 100.0, pos, acct)
    assert out["rejected"] is False
    # Lift +2pp -> target ~11.1%. Add ~2% of $5500 = $110 -> 1 share
    assert out["shares"] >= 1
    assert "Raise from" in out["display"]


def test_add_capped_at_25pct_max_single():
    # Position already 24% -> can only add 1pp to fit
    pos = Position("NVDA", 240, 100)  # 240 * 100 = 24,000
    acct = _account(cash=76_000, positions=[pos])  # pv = 100,000
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("add", "NVDA", 100.0, pos, acct,
                                    gate=_gate(score=3))
    assert out["downsized"] is True
    assert out["target_weight_pct"] <= 25.0


def test_add_rejected_when_already_at_cap():
    pos = Position("META", 10, 100)  # $1000 position
    acct = _account(cash=0, positions=[pos])  # pv = $1000 -> META at 100%
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("add", "META", 100.0, pos, acct)
    assert out["rejected"] is True


# ---------------------------------------------------------------------------
# trim
# ---------------------------------------------------------------------------

def test_trim_default_30pct():
    pos = Position("META", 10, 500)  # cost $500
    acct = _account(cash=0, positions=[pos])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("trim", "META", 700.0, pos, acct)
    # 30% of 10 shares = 3 shares
    assert out["shares"] == 3
    assert out["dollars"] == 2100.0
    # Unrealized P&L: (700 - 500) * 3 = 600
    assert out["unrealized_pnl_on_action"] == 600.0
    assert "Trim 30%" in out["display"]


def test_trim_severe_takes_half():
    pos = Position("META", 10, 500)
    acct = _account(cash=0, positions=[pos])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("trim", "META", 700.0, pos, acct, severity="severe")
    assert out["shares"] == 5
    assert "Trim 50%" in out["display"]


def test_trim_rejected_without_position():
    acct = _account(cash=0, positions=[])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("trim", "META", 700.0, None, acct)
    assert out["rejected"] is True


# ---------------------------------------------------------------------------
# sell
# ---------------------------------------------------------------------------

def test_sell_full_exit():
    pos = Position("LEU", 2, 304)
    acct = _account(cash=100, positions=[pos])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("sell", "LEU", 180.0, pos, acct)
    assert out["shares"] == 2
    assert out["dollars"] == 360.0
    # Loss: (180 - 304) * 2 = -248
    assert out["unrealized_pnl_on_action"] == -248.0
    assert out["target_weight_pct"] == 0.0
    assert "Exit full position" in out["display"]


def test_sell_rejected_without_position():
    acct = _account(cash=0, positions=[])
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("sell", "META", 700.0, None, acct)
    assert out["rejected"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_missing_price_rejects():
    acct = _account(cash=5_000)
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("new_buy", "NVDA", None, None, acct)
    assert out["rejected"] is True
    assert "price" in out["rejection_reason"].lower()


def test_unknown_action_rejects():
    acct = _account(cash=5_000)
    with patch.object(sizing, "load_risk_profile", return_value=_risk()):
        out = sizing.compute_size("watch", "NVDA", 100.0, None, acct)
    assert out["rejected"] is True
    assert "unsupported" in out["rejection_reason"].lower()
