"""Tests for app.portfolio.rec_history."""
from pathlib import Path

from app.portfolio import rec_history


def _brief(*recs) -> dict:
    primary = recs[0] if recs else None
    return {"primary_action": primary, "secondary_actions": list(recs[1:])}


def _rec(rec_id, ticker="META", action="trim") -> dict:
    return {
        "rec_id": rec_id, "ticker": ticker, "action": action,
        "size": {"display": "Trim 30% -> 3 shares -> $1,800 freed",
                  "shares": 3, "dollars": 1800},
    }


def test_record_pending_writes_and_loads(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    brief = _brief(_rec("aaa11111"))
    added = rec_history.record_pending(brief, path=p)
    assert len(added) == 1
    assert added[0]["status"] == "pending"
    assert added[0]["ticker"] == "META"
    loaded = rec_history.load(p)
    assert len(loaded) == 1
    assert loaded[0]["rec_id"] == "aaa11111"


def test_record_pending_dedupes_by_rec_id(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    brief = _brief(_rec("aaa11111"))
    rec_history.record_pending(brief, path=p)
    added = rec_history.record_pending(brief, path=p)
    assert added == []
    assert len(rec_history.load(p)) == 1


def test_secondary_actions_also_recorded(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    brief = _brief(_rec("aaa11111"), _rec("bbb22222", "NVDA", "buy"))
    added = rec_history.record_pending(brief, path=p)
    assert len(added) == 2
    assert {e["ticker"] for e in added} == {"META", "NVDA"}


def test_update_status_accept(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    rec_history.record_pending(_brief(_rec("aaa11111")), path=p)
    entry = rec_history.update_status(
        "aaa11111", "accepted",
        executed_price=614.50, executed_shares=3, path=p,
    )
    assert entry["status"] == "accepted"
    assert entry["executed_price"] == 614.50
    assert entry["executed_shares"] == 3
    assert entry["resolved_at"] is not None


def test_update_status_reject_records_reason(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    rec_history.record_pending(_brief(_rec("aaa11111")), path=p)
    entry = rec_history.update_status("aaa11111", "rejected",
                                       user_reason="still bullish on AI ad spend",
                                       path=p)
    assert entry["user_reason"] == "still bullish on AI ad spend"


def test_update_status_counter(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    rec_history.record_pending(_brief(_rec("aaa11111")), path=p)
    entry = rec_history.update_status(
        "aaa11111", "counter",
        counter_proposal={"action": "hold", "shares": None, "reason": "wait one week"},
        user_reason="wait one week",
        path=p,
    )
    assert entry["status"] == "counter"
    assert entry["counter_proposal"]["action"] == "hold"


def test_update_status_unknown_rec_returns_none(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    assert rec_history.update_status("zzzz9999", "rejected",
                                       user_reason="no", path=p) is None


def test_update_status_rejects_bad_status(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    rec_history.record_pending(_brief(_rec("aaa11111")), path=p)
    try:
        rec_history.update_status("aaa11111", "executed", path=p)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_pending_filter(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    rec_history.record_pending(
        _brief(_rec("aaa11111"), _rec("bbb22222", "NVDA", "buy")), path=p)
    rec_history.update_status("aaa11111", "accepted",
                                executed_price=600, executed_shares=3, path=p)
    pend = rec_history.pending(path=p)
    assert [e["rec_id"] for e in pend] == ["bbb22222"]
