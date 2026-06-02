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


# ---------------------------------------------------------------------------
# CIK index TTL + persistence + loud failure
# ---------------------------------------------------------------------------

def test_cik_cache_fresh_hit_skips_network(monkeypatch, tmp_path):
    """A CIK cache younger than its TTL must NOT re-hit SEC.

    Regression guard: without persistence, every fresh container made
    one live SEC call per build; a single 403 then cascaded into the
    silent score-0 for every insider lookup.
    """
    import time
    from app.data import filings
    monkeypatch.setattr(filings, "_CIK_CACHE_PATH", tmp_path / "ticker_cik_cache.json")
    monkeypatch.setattr(filings, "LAST_CIK_ERROR", None)
    # Pre-populate a fresh cache (age 1 second).
    fresh_payload = {"fetched_at": time.time() - 1.0,
                     "mapping": {"AAA": "0000000001"}}
    filings._CIK_CACHE_PATH.write_text(__import__("json").dumps(fresh_payload))

    calls = {"n": 0}
    def _no_network(*a, **kw):
        calls["n"] += 1
        raise AssertionError("network was hit despite a fresh cache")
    monkeypatch.setattr(filings.httpx, "get", _no_network)

    mapping = filings._ticker_to_cik()
    assert mapping == {"AAA": "0000000001"}
    assert calls["n"] == 0
    assert filings.LAST_CIK_ERROR is None


def test_cik_cache_expired_triggers_refetch(monkeypatch, tmp_path):
    """A CIK cache older than the TTL must re-fetch from SEC."""
    import json, time
    from app.data import filings
    monkeypatch.setattr(filings, "_CIK_CACHE_PATH", tmp_path / "ticker_cik_cache.json")
    monkeypatch.setattr(filings, "LAST_CIK_ERROR", None)
    # Cache older than 7 days (TTL).
    stale_payload = {"fetched_at": time.time() - (8 * 24 * 3600),
                     "mapping": {"OLD": "0000000999"}}
    filings._CIK_CACHE_PATH.write_text(json.dumps(stale_payload))

    fake_rows = {"a": {"ticker": "FRESH", "cik_str": 42}}
    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return fake_rows
    monkeypatch.setattr(filings.httpx, "get", lambda *a, **kw: _R())

    out = filings._ticker_to_cik()
    assert out == {"FRESH": "0000000042"}
    # The fresh result was persisted with a current timestamp.
    saved = json.loads(filings._CIK_CACHE_PATH.read_text())
    assert saved["mapping"] == {"FRESH": "0000000042"}
    assert saved["fetched_at"] > time.time() - 60


def test_cik_failed_fetch_no_cache_raises_loud(monkeypatch, tmp_path):
    """Empty cache + network failure must raise CIKLookupError + record
    the reason -- never return a silent empty mapping."""
    from app.data import filings
    monkeypatch.setattr(filings, "_CIK_CACHE_PATH", tmp_path / "no_cache.json")
    monkeypatch.setattr(filings, "LAST_CIK_ERROR", None)

    def _explode(*a, **kw):
        raise filings.httpx.ConnectError("boom")
    monkeypatch.setattr(filings.httpx, "get", _explode)

    import pytest
    with pytest.raises(filings.CIKLookupError) as exc_info:
        filings._ticker_to_cik()
    assert "CIK index fetch failed" in str(exc_info.value)
    assert filings.LAST_CIK_ERROR is not None
    assert "ConnectError" in filings.LAST_CIK_ERROR


def test_cik_failed_fetch_with_stale_cache_serves_stale(monkeypatch, tmp_path):
    """If the network fails but a stale cache exists, return it (with a
    warning) rather than raise. Stale beats nothing."""
    import json, time
    from app.data import filings
    monkeypatch.setattr(filings, "_CIK_CACHE_PATH", tmp_path / "ticker_cik_cache.json")
    monkeypatch.setattr(filings, "LAST_CIK_ERROR", None)
    stale_payload = {"fetched_at": time.time() - (30 * 24 * 3600),
                     "mapping": {"OLD": "0000000999"}}
    filings._CIK_CACHE_PATH.write_text(json.dumps(stale_payload))

    def _explode(*a, **kw):
        raise filings.httpx.ConnectError("boom")
    monkeypatch.setattr(filings.httpx, "get", _explode)

    out = filings._ticker_to_cik()
    assert out == {"OLD": "0000000999"}  # stale, but better than nothing
    # The error is still recorded so the diagnostic surfaces.
    assert filings.LAST_CIK_ERROR is not None


def test_insider_diagnostics_loud_on_cik_failure(monkeypatch, tmp_path):
    """A CIK failure during a per-ticker insider fetch must surface in
    insider.diagnostics() with a non-empty reason -- not a silent 0."""
    from app.data import filings, insider
    monkeypatch.setattr(filings, "_CIK_CACHE_PATH", tmp_path / "no_cache.json")
    monkeypatch.setattr(filings, "LAST_CIK_ERROR", None)
    insider.reset_diagnostics()

    def _explode(*a, **kw):
        raise filings.httpx.ConnectError("boom")
    monkeypatch.setattr(filings.httpx, "get", _explode)

    # Force the uncached path (skip the form4_cache.json shortcut).
    monkeypatch.setattr(insider, "_CACHE_PATH", tmp_path / "form4_cache.json")
    txns, ok = insider._recent_form4_transactions_uncached("AAA")
    assert txns == [] and ok is False

    diag = insider.diagnostics()
    assert diag["cik_index_error"] is not None
    assert "AAA" in diag["ticker_errors"]
    assert "ConnectError" in diag["ticker_errors"]["AAA"]
    assert diag["tickers_unavailable"] == 1


def test_insider_success_clears_loud_diagnostic(monkeypatch, tmp_path):
    """A successful fetch clears the per-ticker error so a stale failure
    doesn't keep showing up."""
    import json, time
    from app.data import filings, insider
    monkeypatch.setattr(filings, "_CIK_CACHE_PATH", tmp_path / "ticker_cik_cache.json")
    fresh_payload = {"fetched_at": time.time() - 1.0,
                     "mapping": {"AAA": "0000000001"}}
    filings._CIK_CACHE_PATH.write_text(json.dumps(fresh_payload))
    monkeypatch.setattr(insider, "_CACHE_PATH", tmp_path / "form4_cache.json")
    insider.LAST_FETCH_ERRORS["AAA"] = "previous failure"

    class _OK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"filings": {"recent": {
            "form": [], "filingDate": [], "accessionNumber": [],
            "primaryDocument": [],
        }}}
    monkeypatch.setattr(insider.httpx, "get", lambda *a, **kw: _OK())

    txns, ok = insider._recent_form4_transactions_uncached("AAA")
    assert ok is True
    # The previous error must be cleared on success.
    assert "AAA" not in insider.LAST_FETCH_ERRORS
