"""Tests for the 24h form 4 disk cache wrapping recent_form4_transactions."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.data import insider


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point the cache at a tmp file for every test."""
    monkeypatch.setattr(insider, "_CACHE_PATH", tmp_path / "form4_cache.json")
    yield


def _write_cache(path, key, fetched_at, data):
    path.write_text(json.dumps({key: {"fetched_at": fetched_at, "data": data}}))


def test_fresh_hit_does_not_call_fetcher(monkeypatch):
    calls = {"n": 0}

    def _stub(ticker, days, max_filings):
        calls["n"] += 1
        return [{"shares": 1}], True

    monkeypatch.setattr(insider, "_recent_form4_transactions_uncached", _stub)
    fresh = datetime.now(timezone.utc).isoformat()
    _write_cache(insider._CACHE_PATH, "AAA|30|recent_form4_transactions",
                 fresh, [{"shares": 99}])
    out = insider.recent_form4_transactions("AAA", days=30)
    assert out == [{"shares": 99}]
    assert calls["n"] == 0


def test_stale_miss_triggers_fetch(monkeypatch):
    calls = {"n": 0}

    def _stub(ticker, days, max_filings):
        calls["n"] += 1
        return [{"shares": 7}], True

    monkeypatch.setattr(insider, "_recent_form4_transactions_uncached", _stub)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    _write_cache(insider._CACHE_PATH, "AAA|30|recent_form4_transactions",
                 stale, [{"shares": 1}])
    out = insider.recent_form4_transactions("AAA", days=30)
    assert calls["n"] == 1
    assert out == [{"shares": 7}]
    # The new (fresh) result was written back.
    cache = json.loads(insider._CACHE_PATH.read_text())
    assert cache["AAA|30|recent_form4_transactions"]["data"] == [{"shares": 7}]


def test_fetch_failure_with_stale_cache_serves_stale(monkeypatch):
    def _stub(ticker, days, max_filings):
        return [], False  # genuine fetch failure

    monkeypatch.setattr(insider, "_recent_form4_transactions_uncached", _stub)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    _write_cache(insider._CACHE_PATH, "AAA|30|recent_form4_transactions",
                 stale, [{"shares": 42}])
    out = insider.recent_form4_transactions("AAA", days=30)
    assert out == [{"shares": 42}]


def test_fetch_failure_with_no_cache_returns_empty(monkeypatch):
    def _stub(ticker, days, max_filings):
        return [], False

    monkeypatch.setattr(insider, "_recent_form4_transactions_uncached", _stub)
    assert not insider._CACHE_PATH.exists()
    out = insider.recent_form4_transactions("AAA", days=30)
    assert out == []


def test_malformed_cache_file_treated_as_empty(monkeypatch):
    calls = {"n": 0}

    def _stub(ticker, days, max_filings):
        calls["n"] += 1
        return [{"shares": 5}], True

    monkeypatch.setattr(insider, "_recent_form4_transactions_uncached", _stub)
    insider._CACHE_PATH.write_text("{garbage not json")
    out = insider.recent_form4_transactions("AAA", days=30)
    assert calls["n"] == 1
    assert out == [{"shares": 5}]


def test_cache_keyed_by_ticker_days_and_function(monkeypatch):
    """Different (ticker, days, fn) keys do not collide."""
    counts: dict[str, int] = {}

    def _stub(ticker, days, max_filings):
        counts[ticker + "|" + str(days)] = counts.get(ticker + "|" + str(days), 0) + 1
        return [{"t": ticker, "d": days}], True

    monkeypatch.setattr(insider, "_recent_form4_transactions_uncached", _stub)
    insider.recent_form4_transactions("AAA", days=30)
    insider.recent_form4_transactions("AAA", days=30)   # cached hit
    insider.recent_form4_transactions("AAA", days=60)   # different window
    insider.recent_form4_transactions("BBB", days=30)   # different ticker
    assert counts == {"AAA|30": 1, "AAA|60": 1, "BBB|30": 1}
