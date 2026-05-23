"""Tests for the persistent idea queue store."""
from app.portfolio import idea_queue


def _idea(ticker, rank, score, why="why"):
    return {"ticker": ticker, "rank": rank, "score": score, "why": why}


def test_load_missing_returns_empty(tmp_path):
    assert idea_queue.load(tmp_path / "nope.yaml") == []


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "q.yaml"
    idea_queue.save([{"ticker": "PLTR", "verdict": "open"}], p)
    assert idea_queue.load(p)[0]["ticker"] == "PLTR"


def test_find_is_case_insensitive():
    queue = [{"ticker": "PLTR"}]
    assert idea_queue.find("pltr", queue)["ticker"] == "PLTR"
    assert idea_queue.find("AMD", queue) is None


def test_sync_upserts_and_refreshes_rank(tmp_path):
    p = tmp_path / "q.yaml"
    idea_queue.sync_from_funnel([_idea("PLTR", 1, 9.0)], path=p)
    q = idea_queue.sync_from_funnel([_idea("PLTR", 4, 5.0, "new why")], path=p)
    entry = idea_queue.find("PLTR", q)
    assert entry["last_rank"] == 4
    assert entry["last_score"] == 5.0
    assert entry["last_why"] == "new why"
    assert len(q) == 1


def test_sync_preserves_verdict_on_existing_entry(tmp_path):
    p = tmp_path / "q.yaml"
    idea_queue.set_verdict("PLTR", "interested", "like it", p)
    q = idea_queue.sync_from_funnel([_idea("PLTR", 2, 7.0)], path=p)
    entry = idea_queue.find("PLTR", q)
    assert entry["verdict"] == "interested"
    assert entry["user_note"] == "like it"
    assert entry["last_rank"] == 2


def test_sync_keeps_offfunnel_entries(tmp_path):
    p = tmp_path / "q.yaml"
    idea_queue.set_verdict("AMD", "watching", None, p)
    q = idea_queue.sync_from_funnel([_idea("PLTR", 1, 9.0)], path=p)
    assert idea_queue.find("AMD", q) is not None
    assert idea_queue.find("PLTR", q) is not None


def test_set_verdict_creates_entry_and_stamps_time(tmp_path):
    p = tmp_path / "q.yaml"
    entry = idea_queue.set_verdict("nvda", "pass", "too extended", p)
    assert entry["ticker"] == "NVDA"
    assert entry["verdict"] == "pass"
    assert entry["verdict_at"] is not None
    assert entry["user_note"] == "too extended"
    assert entry["idea_id"] == idea_queue.idea_id("NVDA")


def test_set_verdict_rejects_unknown_verdict(tmp_path):
    try:
        idea_queue.set_verdict("PLTR", "maybe", None, tmp_path / "q.yaml")
        assert False, "should have raised"
    except ValueError:
        pass


def test_verdict_map_only_decided(tmp_path):
    p = tmp_path / "q.yaml"
    idea_queue.set_verdict("PLTR", "interested", None, p)
    idea_queue.set_verdict("AMD", "pass", None, p)
    q = idea_queue.sync_from_funnel([_idea("NVDA", 1, 9.0)], path=p)
    vm = idea_queue.verdict_map(q)
    assert vm == {"PLTR": "interested", "AMD": "pass"}  # NVDA stays 'open'


# --- aging / prune ----------------------------------------------------------

from datetime import date, timedelta


def _entry(ticker, verdict="open", last_seen=None, verdict_at=None,
           user_note=None):
    return {"ticker": ticker.upper(), "idea_id": idea_queue.idea_id(ticker),
            "first_seen": last_seen, "last_seen": last_seen,
            "last_rank": None, "last_score": None, "last_why": None,
            "verdict": verdict, "verdict_at": verdict_at,
            "user_note": user_note}


def test_prune_drops_stale_open_not_in_funnel():
    today = date(2026, 5, 22)
    e = _entry("PLTR", last_seen=(today - timedelta(days=15)).isoformat())
    out, stats = idea_queue.prune([e], set(), today)
    assert out == []
    assert stats == {"dropped_open": 1, "expired_pass": 0}


def test_prune_keeps_stale_open_when_in_todays_funnel():
    today = date(2026, 5, 22)
    e = _entry("PLTR", last_seen=(today - timedelta(days=15)).isoformat())
    out, stats = idea_queue.prune([e], {"PLTR"}, today)
    assert len(out) == 1 and out[0]["ticker"] == "PLTR"
    assert stats == {"dropped_open": 0, "expired_pass": 0}


def test_prune_open_at_14_days_is_kept_not_in_funnel():
    """The drop rule is strictly older than 14 days; exactly 14 stays."""
    today = date(2026, 5, 22)
    e = _entry("PLTR", last_seen=(today - timedelta(days=14)).isoformat())
    out, stats = idea_queue.prune([e], set(), today)
    assert len(out) == 1
    assert stats["dropped_open"] == 0


def test_prune_resets_pass_older_than_90_days():
    today = date(2026, 5, 22)
    old = (today - timedelta(days=91)).isoformat() + "T12:00:00Z"
    e = _entry("PLTR", verdict="pass", verdict_at=old, user_note="not now")
    out, stats = idea_queue.prune([e], set(), today)
    assert len(out) == 1
    reset = out[0]
    assert reset["verdict"] == "open"
    assert reset["verdict_at"] is None
    assert reset["user_note"] == "not now"   # user_note unchanged
    assert stats == {"dropped_open": 0, "expired_pass": 1}


def test_prune_keeps_recent_pass():
    today = date(2026, 5, 22)
    recent = (today - timedelta(days=89)).isoformat() + "T12:00:00Z"
    e = _entry("PLTR", verdict="pass", verdict_at=recent)
    out, stats = idea_queue.prune([e], set(), today)
    assert out[0]["verdict"] == "pass"
    assert stats["expired_pass"] == 0


def test_prune_never_expires_interested():
    today = date(2026, 5, 22)
    very_old = (today - timedelta(days=365)).isoformat() + "T12:00:00Z"
    e = _entry("PLTR", verdict="interested", verdict_at=very_old)
    out, stats = idea_queue.prune([e], set(), today)
    assert out == [e]
    assert stats == {"dropped_open": 0, "expired_pass": 0}


def test_prune_never_expires_watching():
    today = date(2026, 5, 22)
    very_old = (today - timedelta(days=365)).isoformat() + "T12:00:00Z"
    e = _entry("PLTR", verdict="watching", verdict_at=very_old)
    out, stats = idea_queue.prune([e], set(), today)
    assert out == [e]
