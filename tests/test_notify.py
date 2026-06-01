"""Tests for app.notify."""
import json
from pathlib import Path

from app import notify


def _brief(primary=None, secondary=None) -> dict:
    return {"primary_action": primary, "secondary_actions": secondary or []}


def _rec(rec_id, ticker="META", action="buy") -> dict:
    return {
        "rec_id": rec_id, "ticker": ticker, "action": action,
        "entry": "~$520.00", "stop": "$495", "target": "$580",
        "conviction": 4, "thesis": "Breakout on volume.",
        "evidence": ["20d high", "RSI 62"],
        "size": {"display": "Buy 5 sh @ $520 -> $2,600"},
    }


def test_sidecar_writes_only_new_recs(tmp_path: Path):
    p = tmp_path / ".new_recs.json"
    primary = _rec("aaa11111", "META", "buy")
    secondary = _rec("bbb22222", "AAPL", "trim")
    brief = _brief(primary=primary, secondary=[secondary])
    added = [{"rec_id": "aaa11111"}]
    payload = notify.write_sidecar(brief, added, path=p)
    assert len(payload) == 1
    assert payload[0]["ticker"] == "META"
    assert payload[0]["_slot"] == "primary"
    on_disk = json.loads(p.read_text())
    assert on_disk == payload


def test_sidecar_empty_when_nothing_added(tmp_path: Path):
    p = tmp_path / ".new_recs.json"
    brief = _brief(primary=_rec("aaa11111"))
    payload = notify.write_sidecar(brief, [], path=p)
    assert payload == []
    assert json.loads(p.read_text()) == []


def test_sidecar_overwrites_stale_file(tmp_path: Path):
    p = tmp_path / ".new_recs.json"
    p.write_text(json.dumps([{"rec_id": "stale", "ticker": "OLD"}]))
    notify.write_sidecar(_brief(), [], path=p)
    assert json.loads(p.read_text()) == []


def test_sidecar_tags_secondary_slot(tmp_path: Path):
    p = tmp_path / ".new_recs.json"
    secondary = _rec("bbb22222", "AAPL", "trim")
    brief = _brief(primary=None, secondary=[secondary])
    payload = notify.write_sidecar(brief, [{"rec_id": "bbb22222"}], path=p)
    assert payload[0]["_slot"] == "secondary"
    assert payload[0]["ticker"] == "AAPL"
