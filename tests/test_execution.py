"""Tests for the Phase 4 accept -> portfolio.yaml mutation path.

Verifies all four rec actions plus the cost-basis and full-sell semantics
inside ``app.portfolio.rec_action_handler``.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.portfolio import rec_action_handler, rec_history, store
from app.portfolio.store import Account, Position


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    """Redirect rec_history, portfolio, and portfolio_history to tmp_path."""
    rh = tmp_path / "rec_history.yaml"
    rh.write_text("[]\n")
    p_yaml = tmp_path / "portfolio.yaml"
    p_yaml.write_text(yaml.safe_dump({
        "account": {"cash": 5000.0, "total_value": 0.0, "currency": "USD"},
        "positions": [{"ticker": "META", "shares": 10, "cost_basis": 500.0}],
    }))
    p_hist = tmp_path / "portfolio_history.yaml"
    p_hist.write_text("[]\n")

    monkeypatch.setattr(rec_history, "HISTORY_PATH", rh)
    monkeypatch.setattr("app.portfolio.trade_log.HISTORY_PATH", p_hist)
    # store.load / store.save honor settings.portfolio_path
    from app.config import settings
    monkeypatch.setattr(settings, "portfolio_path", p_yaml)
    return tmp_path


def _seed_pending(rec_id: str, ticker: str, action: str, shares: int = 1,
                   dollars: float = 100.0) -> dict:
    """Drop a pending entry into rec_history.yaml; return it."""
    history = rec_history.load()
    entry = {
        "rec_id": rec_id,
        "date": "2026-05-18",
        "ticker": ticker,
        "action": action,
        "size": {"display": "x", "shares": shares, "dollars": dollars},
        "status": "pending",
        "user_reason": None, "counter_proposal": None,
        "executed_price": None, "executed_shares": None,
        "resolved_at": None, "created_at": "2026-05-18T11:00:00Z",
    }
    history.append(entry)
    rec_history.save(history)
    return entry


def _body(rec_id: str, **fields) -> str:
    lines = [f"### Rec ID\n\n{rec_id}\n"]
    for k, v in fields.items():
        label = {
            "executed_price": "Executed price (per share)",
            "executed_shares": "Executed shares",
            "reason": "Reason",
            "counter_action": "Counter action",
            "counter_shares": "Counter shares",
            "notes": "Notes",
        }[k]
        lines.append(f"### {label}\n\n{v}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Accept paths
# ---------------------------------------------------------------------------

def test_accept_buy_new_position_updates_yaml(patched_paths):
    _seed_pending("buy11111", "NVDA", "buy", shares=3, dollars=435.0)
    body = _body("buy11111", executed_price="145.20", executed_shares="3")
    result = rec_action_handler.apply_from_issue("Accept rec buy11111", body)
    assert result["kind"] == "accept"
    acct = store.load()
    nvda = acct.position("NVDA")
    assert nvda is not None and nvda.shares == 3
    assert abs(nvda.cost_basis - 145.20) < 1e-6
    assert acct.cash == 5000 - 3 * 145.20


def test_accept_add_weighted_average_cost_basis(patched_paths):
    _seed_pending("add22222", "META", "add", shares=2, dollars=1228.0)
    body = _body("add22222", executed_price="614.00", executed_shares="2")
    rec_action_handler.apply_from_issue("Accept rec add22222", body)
    acct = store.load()
    meta = acct.position("META")
    # (10 * 500 + 2 * 614) / 12 = 6228 / 12 = 519.00
    assert meta is not None and meta.shares == 12
    assert abs(meta.cost_basis - 519.0) < 1e-4
    assert acct.cash == 5000 - 2 * 614.00


def test_accept_trim_partial_sell_keeps_cost_basis(patched_paths):
    _seed_pending("trim33333", "META", "trim", shares=3, dollars=1842.0)
    body = _body("trim33333", executed_price="614.00", executed_shares="3")
    rec_action_handler.apply_from_issue("Accept rec trim33333", body)
    acct = store.load()
    meta = acct.position("META")
    assert meta is not None and meta.shares == 7
    assert meta.cost_basis == 500.0   # cost basis unchanged on trim
    assert acct.cash == 5000 + 3 * 614.00


def test_accept_sell_full_removes_position(patched_paths):
    _seed_pending("sell4444", "META", "sell", shares=10, dollars=6140.0)
    body = _body("sell4444", executed_price="614.00", executed_shares="10")
    rec_action_handler.apply_from_issue("Accept rec sell4444", body)
    acct = store.load()
    assert acct.position("META") is None
    assert acct.cash == 5000 + 10 * 614.00


def test_accept_uses_executed_not_recommended(patched_paths):
    # rec said 3 shares @ ~$614 but the user fills 5 @ $610.
    _seed_pending("buy55555", "META", "add", shares=3, dollars=1842.0)
    body = _body("buy55555", executed_price="610.00", executed_shares="5")
    rec_action_handler.apply_from_issue("Accept rec buy55555", body)
    acct = store.load()
    meta = acct.position("META")
    assert meta.shares == 15
    # (10 * 500 + 5 * 610) / 15 = 8050 / 15 = 536.67
    assert abs(meta.cost_basis - 536.6667) < 0.001
    assert acct.cash == 5000 - 5 * 610.00


# ---------------------------------------------------------------------------
# Reject / Counter paths leave portfolio.yaml alone
# ---------------------------------------------------------------------------

def test_reject_does_not_touch_portfolio(patched_paths):
    _seed_pending("rej66666", "META", "trim")
    before = store.load()
    body = _body("rej66666", reason="still bullish on AI ad spend")
    rec_action_handler.apply_from_issue("Reject rec rej66666", body)
    after = store.load()
    assert before.cash == after.cash
    assert len(before.positions) == len(after.positions)
    entry = rec_history.find("rej66666")
    assert entry["status"] == "rejected"
    assert "bullish" in entry["user_reason"]


def test_counter_does_not_touch_portfolio(patched_paths):
    _seed_pending("ctr77777", "META", "trim")
    before = store.load()
    body = _body("ctr77777", counter_action="hold",
                  counter_shares="0", reason="wait a week")
    rec_action_handler.apply_from_issue("Counter rec ctr77777", body)
    after = store.load()
    assert before.cash == after.cash
    entry = rec_history.find("ctr77777")
    assert entry["status"] == "counter"
    assert entry["counter_proposal"]["action"] == "hold"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_accept_unknown_rec_id_raises(patched_paths):
    body = _body("zzzz9999", executed_price="100", executed_shares="1")
    with pytest.raises(ValueError, match="not found"):
        rec_action_handler.apply_from_issue("Accept rec zzzz9999", body)


def test_accept_missing_executed_fields_raises(patched_paths):
    _seed_pending("buy88888", "NVDA", "buy")
    body = _body("buy88888", executed_price="145.00")  # no shares
    with pytest.raises(ValueError):
        rec_action_handler.apply_from_issue("Accept rec buy88888", body)


def test_accept_hold_or_watch_skips_portfolio_mutation(patched_paths):
    _seed_pending("hold99999", "PLTR", "hold")
    before = store.load()
    body = _body("hold99999", executed_price="35.00", executed_shares="0")
    result = rec_action_handler.apply_from_issue("Accept rec hold99999", body)
    assert "no portfolio mutation" in result["summary"]
    assert store.load().cash == before.cash
