"""Tests for the additive parallel price prefetch (Phase 1).

``prices.prefetch`` only warms the in-memory ``_HISTORY_CACHE``; it must not
change ``quote`` / ``technicals`` behavior, must de-dupe, and must never raise.
"""
import pandas as pd
import pytest

from app.data import prices


@pytest.fixture(autouse=True)
def _isolate_history_cache():
    """Keep the module-global cache from leaking between tests."""
    prices._HISTORY_CACHE.clear()
    yield
    prices._HISTORY_CACHE.clear()


def _fake_df() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=30, freq="B")
    return pd.DataFrame(
        {
            "Open": [10.0] * 30,
            "High": [11.0] * 30,
            "Low": [9.0] * 30,
            "Close": [10.0 + i * 0.1 for i in range(30)],
            "Volume": [1000] * 30,
        },
        index=idx,
    )


def test_prefetch_warms_cache_and_sequential_reads_hit_it(monkeypatch):
    calls: list[str] = []

    def fake_source(ticker: str) -> pd.DataFrame:
        calls.append(ticker)
        return _fake_df()

    monkeypatch.setattr(prices, "_SOURCES", [("fake", fake_source)])
    # Don't touch the on-disk persistent cache from the test.
    monkeypatch.setattr(prices, "_save_to_persistent_cache", lambda *a, **k: None)

    prices.prefetch(["AAPL", "MSFT", "NVDA"])

    # Every ticker is now cached, and the source was hit exactly once each.
    assert set(prices._HISTORY_CACHE) == {"AAPL", "MSFT", "NVDA"}
    assert sorted(calls) == ["AAPL", "MSFT", "NVDA"]

    # The sequential paths after a prefetch must serve entirely from cache:
    # no additional source round-trips for an already-warmed ticker.
    calls.clear()
    q = prices.quote("AAPL", fast=True)
    t = prices.technicals("AAPL")
    assert q.price is not None
    assert t.get("error") is None
    assert calls == []  # no new fetches - the warm cache was reused


def test_prefetch_dedupes_and_normalizes_case(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        prices, "_SOURCES",
        [("fake", lambda t: (calls.append(t) or _fake_df()))],
    )
    # Mixed case + duplicates + falsy entries collapse to a single fetch.
    prices.prefetch(["aapl", "AAPL", "Aapl", "", None])
    assert set(prices._HISTORY_CACHE) == {"AAPL"}
    assert calls == ["AAPL"]


def test_prefetch_caches_misses_when_sources_return_no_data(monkeypatch):
    # Real sources return None on failure (they're _retry-wrapped); the miss
    # is cached as (ts, None, None) just like the sequential path.
    monkeypatch.setattr(prices, "_SOURCES", [("none", lambda t: None)])
    prices.prefetch(["AAPL"])
    cached = prices._HISTORY_CACHE.get("AAPL")
    assert cached is not None and cached[1] is None and cached[2] is None


def test_prefetch_never_raises_even_if_every_source_raises(monkeypatch):
    def boom(ticker: str):
        raise RuntimeError("source exploded")

    monkeypatch.setattr(prices, "_SOURCES", [("boom", boom)])
    # Must return cleanly despite every per-ticker fetch raising.
    prices.prefetch(["AAPL", "MSFT"])


def test_prefetch_empty_list_is_noop():
    prices.prefetch([])
    assert prices._HISTORY_CACHE == {}
