from pathlib import Path

import pytest

from app.portfolio import store, trade_log


def _account(cash: float = 1000.0, positions=None) -> store.Account:
    return store.Account(cash=cash, total_value=0, currency="USD",
                          positions=positions or [])


def test_parse_buy():
    body = """### Action

BUY

### Ticker

NVDA

### Shares

3

### Fill price (per share)

145.50

### Cash amount

_No response_

### Date

2026-05-17

### Notes

_No response_
"""
    t = trade_log.parse(body)
    assert t.action == "BUY"
    assert t.ticker == "NVDA"
    assert t.shares == 3
    assert t.price == 145.50
    assert t.amount is None
    assert t.trade_date == "2026-05-17"


def test_parse_deposit():
    body = """### Action

DEPOSIT

### Ticker

_No response_

### Shares

_No response_

### Fill price (per share)

_No response_

### Cash amount

$500

### Date

_No response_

### Notes

_No response_
"""
    t = trade_log.parse(body)
    assert t.action == "DEPOSIT"
    assert t.amount == 500


def test_parse_invalid_action():
    with pytest.raises(ValueError):
        trade_log.parse("### Action\n\nFOO\n")


def test_apply_buy_creates_new_position():
    acct = _account(cash=1000)
    trade = trade_log.Trade("BUY", "NVDA", 2, 100.0, None, "2026-05-17")
    out, summary = trade_log.apply(trade, acct)
    assert out.cash == 800
    pos = out.position("NVDA")
    assert pos.shares == 2
    assert pos.cost_basis == 100.0
    assert "BUY 2" in summary


def test_apply_buy_averages_cost_basis():
    acct = _account(cash=1000, positions=[store.Position("NVDA", 2, 100.0)])
    trade = trade_log.Trade("BUY", "NVDA", 3, 200.0, None, "2026-05-17")
    out, _ = trade_log.apply(trade, acct)
    pos = out.position("NVDA")
    assert pos.shares == 5
    assert abs(pos.cost_basis - 160.0) < 1e-6


def test_apply_buy_rejects_insufficient_cash():
    acct = _account(cash=100)
    trade = trade_log.Trade("BUY", "NVDA", 2, 100.0, None, "2026-05-17")
    with pytest.raises(ValueError):
        trade_log.apply(trade, acct)


def test_apply_sell_partial():
    acct = _account(cash=0, positions=[store.Position("META", 10, 500)])
    trade = trade_log.Trade("SELL", "META", 4, 600.0, None, "2026-05-17")
    out, _ = trade_log.apply(trade, acct)
    assert out.cash == 2400
    pos = out.position("META")
    assert pos.shares == 6


def test_apply_sell_full_removes_position():
    acct = _account(cash=0, positions=[store.Position("META", 10, 500)])
    trade = trade_log.Trade("SELL", "META", 10, 600.0, None, "2026-05-17")
    out, _ = trade_log.apply(trade, acct)
    assert out.position("META") is None
    assert out.cash == 6000


def test_apply_sell_rejects_overshoot():
    acct = _account(cash=0, positions=[store.Position("META", 10, 500)])
    trade = trade_log.Trade("SELL", "META", 11, 600.0, None, "2026-05-17")
    with pytest.raises(ValueError):
        trade_log.apply(trade, acct)


def test_apply_deposit_withdraw():
    acct = _account(cash=100)
    out, _ = trade_log.apply(trade_log.Trade("DEPOSIT", None, None, None, 500, "2026-05-17"), acct)
    assert out.cash == 600
    out, _ = trade_log.apply(trade_log.Trade("WITHDRAW", None, None, None, 200, "2026-05-17"), out)
    assert out.cash == 400
    with pytest.raises(ValueError):
        trade_log.apply(trade_log.Trade("WITHDRAW", None, None, None, 999, "2026-05-17"), out)
