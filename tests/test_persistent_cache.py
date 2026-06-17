"""Tests for the de-thrashed persistent price cache (Phase 2).

price_cache.json must be read once and written once per process: live fetches
accumulate in an in-memory dict and are flushed a single time. The read-fallback
semantics (prior-run entries + this-run saves, merged) must be unchanged.
"""
import json
import threading

import pytest

from app.data import prices


@pytest.fixture(autouse=True)
def _isolate_persistent_cache(tmp_path, monkeypatch):
    # Point the cache at a temp file and start from clean module state so the
    # process-wide in-memory cache can't leak between tests (or dirty the real
    # price_cache.json via the atexit flush at session end).
    monkeypatch.setattr(prices, "_CACHE_PATH", tmp_path / "price_cache.json")
    prices._PERSISTENT_CACHE = None
    prices._PERSISTENT_DIRTY = False
    yield
    prices._PERSISTENT_CACHE = None
    prices._PERSISTENT_DIRTY = False


def test_save_defers_disk_write_until_flush():
    path = prices._CACHE_PATH
    prices._save_to_persistent_cache("AAPL", {"price": 100.0})
    # The save only touched the in-memory cache - nothing on disk yet.
    assert not path.exists()
    assert prices._PERSISTENT_DIRTY is True

    prices.flush_persistent_cache()
    assert path.exists()
    assert json.loads(path.read_text())["AAPL"]["price"] == 100.0
    assert prices._PERSISTENT_DIRTY is False


def test_reads_disk_only_once(monkeypatch):
    reads = {"n": 0}
    real = prices._read_cache_file

    def counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(prices, "_read_cache_file", counting)
    # A spread of operations that each used to re-read the JSON file.
    prices._save_to_persistent_cache("AAPL", {"price": 1.0})
    prices._save_to_persistent_cache("MSFT", {"price": 2.0})
    prices._from_persistent_cache("AAPL")
    prices._from_persistent_cache("ZZZZ")
    prices._load_persistent_cache()
    assert reads["n"] == 1


def test_writes_disk_only_once_per_flush(monkeypatch):
    writes = {"n": 0}
    real = prices._save_persistent_cache

    def counting(data):
        writes["n"] += 1
        return real(data)

    monkeypatch.setattr(prices, "_save_persistent_cache", counting)
    for t in ("AAPL", "MSFT", "NVDA", "AMD"):
        prices._save_to_persistent_cache(t, {"price": 1.0})
    assert writes["n"] == 0  # no disk write during the saves

    prices.flush_persistent_cache()
    assert writes["n"] == 1  # exactly one write for the whole run

    prices.flush_persistent_cache()  # nothing new -> still one
    assert writes["n"] == 1


def test_flush_noop_when_nothing_saved(monkeypatch):
    writes = {"n": 0}
    monkeypatch.setattr(prices, "_save_persistent_cache",
                        lambda data: writes.__setitem__("n", writes["n"] + 1))
    prices.flush_persistent_cache()
    assert writes["n"] == 0
    assert not prices._CACHE_PATH.exists()


def test_flush_preserves_prior_disk_entries():
    # A cache left by a previous run.
    prices._CACHE_PATH.write_text(json.dumps({"OLD": {"price": 9.0}}))
    prices._PERSISTENT_CACHE = None  # force a reload from the seeded file

    prices._save_to_persistent_cache("NEW", {"price": 5.0})
    prices.flush_persistent_cache()

    merged = json.loads(prices._CACHE_PATH.read_text())
    # Old entry preserved (merge), new entry added - read-fallback unchanged.
    assert merged["OLD"]["price"] == 9.0
    assert merged["NEW"]["price"] == 5.0


def test_from_persistent_cache_sees_prior_and_current():
    prices._CACHE_PATH.write_text(json.dumps({"OLD": {"price": 9.0}}))
    prices._PERSISTENT_CACHE = None

    prices._save_to_persistent_cache("NEW", {"price": 5.0})
    assert prices._from_persistent_cache("OLD") == {"price": 9.0}
    assert prices._from_persistent_cache("new") == {"price": 5.0}  # case-insensitive
    assert prices._from_persistent_cache("MISSING") is None


def test_quote_fallback_uses_saved_entry_when_live_fails(monkeypatch):
    """End-to-end read-fallback: a successful quote saves to the cache, and a
    later quote whose live sources all fail returns that cached price."""
    import pandas as pd

    def _df():
        idx = pd.date_range("2025-01-01", periods=5, freq="B")
        return pd.DataFrame({"Close": [10.0, 11.0, 12.0, 13.0, 14.0]}, index=idx)

    prices._HISTORY_CACHE.clear()
    monkeypatch.setattr(prices, "_SOURCES", [("fake", lambda t: _df())])
    live = prices.quote("AAPL", fast=True)
    assert live.price == 14.0 and live.source == "fake"

    # Now every live source fails AND the in-memory history cache is cold.
    prices._HISTORY_CACHE.clear()
    monkeypatch.setattr(prices, "_SOURCES", [("dead", lambda t: None)])
    fallback = prices.quote("AAPL", fast=True)
    assert fallback.price == 14.0
    assert fallback.source == "cache"
    assert "cached price" in (fallback.error or "")


def test_concurrent_saves_then_flush_are_safe():
    """Saves from many threads + interleaved flushes must not raise (the lock
    guards the flush snapshot against concurrent writers) and must persist
    every entry after a final flush."""
    errors: list[Exception] = []

    def worker(base: int):
        try:
            for i in range(base, base + 200):
                prices._save_to_persistent_cache(f"T{i}", {"price": float(i)})
                if i % 25 == 0:
                    prices.flush_persistent_cache()
        except Exception as e:  # pragma: no cover - only on a locking bug
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(b * 1000,)) for b in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prices.flush_persistent_cache()

    assert errors == []
    on_disk = json.loads(prices._CACHE_PATH.read_text())
    assert len(on_disk) == 6 * 200  # all distinct tickers persisted
