"""Tests for the idea-action issue handler."""
import pytest

from app.portfolio import idea_action_handler, idea_queue


@pytest.fixture(autouse=True)
def _tmp_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(idea_queue, "QUEUE_PATH", tmp_path / "idea_queue.yaml")


def _body(ticker="PLTR", verdict="interested", note="waiting for $30"):
    parts = []
    if ticker is not None:
        parts.append(f"### Ticker\n\n{ticker}")
    if verdict is not None:
        parts.append(f"### Verdict\n\n{verdict}")
    if note is not None:
        parts.append(f"### Note\n\n{note}")
    return "\n\n".join(parts)


def test_apply_records_verdict():
    out = idea_action_handler.apply_from_issue("Idea PLTR", _body())
    assert out["ticker"] == "PLTR"
    assert out["verdict"] == "interested"
    entry = idea_queue.find("PLTR", idea_queue.load())
    assert entry["verdict"] == "interested"
    assert entry["user_note"] == "waiting for $30"


def test_missing_ticker_raises():
    with pytest.raises(ValueError, match="ticker"):
        idea_action_handler.apply_from_issue("Idea", _body(ticker=None))


def test_invalid_verdict_raises():
    with pytest.raises(ValueError, match="verdict"):
        idea_action_handler.apply_from_issue("Idea", _body(verdict="dunno"))


def test_verdict_phrasing_is_normalized():
    out = idea_action_handler.apply_from_issue("Idea", _body(verdict="Watching it"))
    assert out["verdict"] == "watching"


def test_open_verdict_is_rejected():
    with pytest.raises(ValueError):
        idea_action_handler.apply_from_issue("Idea", _body(verdict="open"))


def test_note_is_optional():
    out = idea_action_handler.apply_from_issue("Idea", _body(note=None))
    assert out["verdict"] == "interested"
