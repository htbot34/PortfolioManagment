"""Tests for app.portfolio.rec_history."""
from datetime import date, timedelta
from pathlib import Path

from app.portfolio import rec_history


def _entry(rec_id, day, status, *, user_reason=None, ticker="TT", action="buy"):
    iso = day.isoformat()
    return {
        "rec_id": rec_id, "date": iso, "ticker": ticker, "action": action,
        "size": {"display": "", "shares": 0, "dollars": 0},
        "status": status, "user_reason": user_reason,
        "counter_proposal": None, "executed_price": None,
        "executed_shares": None,
        "resolved_at": None if status == "pending" else iso + "T00:00:00Z",
        "created_at": iso + "T00:00:00Z",
    }


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


def test_update_status_targets_live_pending_when_rec_id_recurs(tmp_path: Path):
    """A recycled rec_id must resolve the live pending entry, not a stale one.

    find() returns the first match; when an already-resolved entry precedes a
    re-proposed pending entry sharing the same rec_id, update_status must still
    flip the pending one and leave the resolved entry untouched. Without the
    _resolve_for_update fix this rejects the older (already-rejected) entry and
    strands the live pending rec -- exactly what left issue #33 open.
    """
    p = tmp_path / "rec_history.yaml"
    older = date.today() - timedelta(days=2)
    newer = date.today() - timedelta(days=1)
    rec_history.save([
        _entry("dup00001", older, "rejected", user_reason="too pricey"),
        _entry("dup00001", newer, "pending"),
    ], p)

    entry = rec_history.update_status(
        "dup00001", "rejected", user_reason="cleanup", path=p)

    # The entry that got resolved is the live (pending) one.
    assert entry is not None
    assert entry["date"] == newer.isoformat()
    assert entry["status"] == "rejected"
    assert entry["user_reason"] == "cleanup"

    loaded = rec_history.load(p)
    older_e = next(e for e in loaded if e["date"] == older.isoformat())
    newer_e = next(e for e in loaded if e["date"] == newer.isoformat())
    # The previously-resolved entry is untouched...
    assert older_e["status"] == "rejected"
    assert older_e["user_reason"] == "too pricey"
    # ...and there is no longer any pending entry for the rec_id.
    assert rec_history.pending(path=p) == []


def test_update_status_falls_back_to_latest_when_none_pending(tmp_path: Path):
    """With no pending match, resolve the most-recent (last) entry, not the first."""
    p = tmp_path / "rec_history.yaml"
    older = date.today() - timedelta(days=2)
    newer = date.today() - timedelta(days=1)
    rec_history.save([
        _entry("dup00002", older, "rejected", user_reason="first"),
        _entry("dup00002", newer, "accepted", user_reason="second"),
    ], p)

    entry = rec_history.update_status(
        "dup00002", "counter",
        counter_proposal={"action": "hold", "shares": None, "reason": "x"},
        path=p)

    assert entry["date"] == newer.isoformat()
    assert entry["status"] == "counter"
    loaded = rec_history.load(p)
    assert next(e for e in loaded if e["date"] == older.isoformat())["status"] == "rejected"


def test_pending_filter(tmp_path: Path):
    p = tmp_path / "rec_history.yaml"
    rec_history.record_pending(
        _brief(_rec("aaa11111"), _rec("bbb22222", "NVDA", "buy")), path=p)
    rec_history.update_status("aaa11111", "accepted",
                                executed_price=600, executed_shares=3, path=p)
    pend = rec_history.pending(path=p)
    assert [e["rec_id"] for e in pend] == ["bbb22222"]
