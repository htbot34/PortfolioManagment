"""Tests for app.research.learning."""
from datetime import date, timedelta

from app.research import learning


def _entry(rec_id, ticker, action, status, days_ago=1, **extra) -> dict:
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    e = {
        "rec_id": rec_id, "date": d, "ticker": ticker, "action": action,
        "status": status, "user_reason": None, "counter_proposal": None,
        "executed_price": None, "executed_shares": None,
        "resolved_at": d + "T12:00:00Z", "created_at": d + "T11:00:00Z",
    }
    e.update(extra)
    return e


def test_no_history_returns_empty():
    out = learning.derive_user_preferences([])
    assert out["soft_vetoes"] == []
    assert out["counter_patterns"] == []


def test_single_rejection_does_not_veto():
    history = [_entry("a", "META", "trim", "rejected", user_reason="bullish")]
    out = learning.derive_user_preferences(history)
    assert out["soft_vetoes"] == []


def test_two_rejections_same_ticker_triggers_veto():
    history = [
        _entry("a", "META", "trim", "rejected", user_reason="bullish"),
        _entry("b", "META", "trim", "rejected", user_reason="hold for AI"),
    ]
    out = learning.derive_user_preferences(history)
    assert len(out["soft_vetoes"]) == 1
    veto = out["soft_vetoes"][0]
    assert veto["ticker"] == "META"
    assert veto["count"] == 2
    assert "bullish" in veto["reasons"]


def test_rejections_outside_lookback_ignored():
    history = [
        _entry("a", "META", "trim", "rejected", days_ago=40, user_reason="old"),
        _entry("b", "META", "trim", "rejected", days_ago=45, user_reason="older"),
    ]
    out = learning.derive_user_preferences(history)
    assert out["soft_vetoes"] == []


def test_accepted_recs_do_not_count_toward_veto():
    history = [
        _entry("a", "META", "trim", "accepted"),
        _entry("b", "META", "trim", "accepted"),
    ]
    out = learning.derive_user_preferences(history)
    assert out["soft_vetoes"] == []


def test_counter_pattern_picks_up_repeats():
    cp = {"action": "trim", "shares": 1, "reason": "half it"}
    history = [
        _entry("a", "META", "trim", "counter", counter_proposal=cp,
                user_reason="half it"),
        _entry("b", "META", "trim", "counter", counter_proposal=cp,
                user_reason="too aggressive"),
    ]
    out = learning.derive_user_preferences(history)
    assert len(out["counter_patterns"]) == 1
    p = out["counter_patterns"][0]
    assert p["ticker"] == "META" and p["count"] == 2
    assert len(p["examples"]) == 2


def test_soft_veto_tickers_extracts_set():
    history = [
        _entry("a", "META", "trim", "rejected"),
        _entry("b", "META", "sell", "rejected"),
        _entry("c", "NVDA", "buy", "rejected"),
    ]
    out = learning.derive_user_preferences(history)
    tickers = learning.soft_veto_tickers(out)
    assert tickers == {"META"}


def test_actions_rejected_deduped_per_ticker():
    history = [
        _entry("a", "META", "trim", "rejected"),
        _entry("b", "META", "sell", "rejected"),
        _entry("c", "META", "trim", "rejected"),
    ]
    out = learning.derive_user_preferences(history)
    assert out["soft_vetoes"][0]["actions_rejected"] == ["sell", "trim"]
