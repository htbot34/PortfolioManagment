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
