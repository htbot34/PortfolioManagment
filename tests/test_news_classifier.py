"""Tests for app.research.news_classifier."""
import json
from pathlib import Path

import pytest

from app.research import news_classifier


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Point the module's cache file at a fresh temp file for every test."""
    monkeypatch.setattr(news_classifier, "CACHE_PATH", tmp_path / "cache.json")
    return tmp_path


def _llm_ok(*classifications):
    """Build a fake llm_client that returns the given classifications."""
    calls = {"n": 0}

    def client(system, user):
        calls["n"] += 1
        return {"classifications": list(classifications)}

    client.calls = calls  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Basic classification
# ---------------------------------------------------------------------------

def test_classifies_a_single_item():
    client = _llm_ok({"index": 1, "direction": "bullish", "magnitude": 4,
                      "durability": "long", "one_line_summary": "Strong quarter"})
    items = [{"headline": "Co beats earnings", "published": "2026-05-18"}]
    out = news_classifier.classify_news_items("CO", items, llm_client=client)
    assert len(out) == 1
    assert out[0]["direction"] == "bullish"
    assert out[0]["magnitude"] == 4
    assert out[0]["durability"] == "long"
    assert out[0]["source"] == "llm"
    assert out[0]["headline"] == "Co beats earnings"
    assert out[0]["published"] == "2026-05-18"


def test_empty_items_returns_empty():
    assert news_classifier.classify_news_items("CO", [], llm_client=_llm_ok()) == []


def test_magnitude_clamped_to_1_5():
    client = _llm_ok({"index": 1, "direction": "bearish", "magnitude": 99,
                      "durability": "short", "one_line_summary": "x"})
    out = news_classifier.classify_news_items("CO", [{"headline": "h"}], llm_client=client)
    assert out[0]["magnitude"] == 5


def test_bad_durability_defaults_to_medium():
    client = _llm_ok({"index": 1, "direction": "bullish", "magnitude": 3,
                      "durability": "forever", "one_line_summary": "x"})
    out = news_classifier.classify_news_items("CO", [{"headline": "h"}], llm_client=client)
    assert out[0]["durability"] == "medium"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_miss_then_hit(isolate_cache):
    client = _llm_ok({"index": 1, "direction": "bullish", "magnitude": 3,
                      "durability": "medium", "one_line_summary": "x"})
    items = [{"headline": "Co news", "published": "2026-05-18"}]
    news_classifier.classify_news_items("CO", items, llm_client=client)
    assert client.calls["n"] == 1
    # Second call: same item -> served from cache, LLM not called again.
    news_classifier.classify_news_items("CO", items, llm_client=client)
    assert client.calls["n"] == 1
    assert (isolate_cache / "cache.json").exists()


def test_keyword_fallback_results_not_cached(isolate_cache):
    """A keyword fallback must not poison the cache - we want to retry the LLM."""
    def failing_client(system, user):
        return None  # simulates content filter / empty result

    items = [{"headline": "Co beats earnings", "published": "2026-05-18"}]
    out = news_classifier.classify_news_items("CO", items, llm_client=failing_client)
    assert out[0]["source"] == "keyword_fallback"
    cache = json.loads((isolate_cache / "cache.json").read_text()) if (isolate_cache / "cache.json").exists() else {}
    assert cache == {}


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def test_batches_at_10_items():
    seen_batch_sizes = []

    def client(system, user):
        # count numbered lines in the user prompt
        n = sum(1 for ln in user.splitlines() if ln.strip()[:2] in
                {f"{i}." for i in range(1, 10)} or ln.strip().startswith("10."))
        seen_batch_sizes.append(n)
        return {"classifications": [
            {"index": i, "direction": "neutral", "magnitude": 1,
             "durability": "short", "one_line_summary": "x"}
            for i in range(1, n + 1)
        ]}

    items = [{"headline": f"item {i}"} for i in range(23)]
    out = news_classifier.classify_news_items("CO", items, llm_client=client)
    assert len(out) == 23
    # 23 items -> batches of 10, 10, 3
    assert seen_batch_sizes == [10, 10, 3]


# ---------------------------------------------------------------------------
# Content-filter / failure fallback
# ---------------------------------------------------------------------------

def test_content_filter_falls_back_to_keywords():
    def filtered_client(system, user):
        return None  # content_filter would surface as None from llm.chat_json

    items = [
        {"headline": "Co beats Q3 and raises guidance"},
        {"headline": "Co faces an investigation"},
        {"headline": "Co holds an analyst day"},
    ]
    out = news_classifier.classify_news_items("CO", items, llm_client=filtered_client)
    assert all(o["source"] == "keyword_fallback" for o in out)
    assert out[0]["direction"] == "bullish"   # "beats", "raises"
    assert out[1]["direction"] == "bearish"   # "investigation"
    assert out[2]["direction"] == "neutral"   # no keywords


def test_llm_exception_falls_back():
    def boom(system, user):
        raise RuntimeError("network down")

    out = news_classifier.classify_news_items(
        "CO", [{"headline": "Co beats earnings"}], llm_client=boom)
    assert out[0]["source"] == "keyword_fallback"
    assert out[0]["direction"] == "bullish"


def test_partial_llm_result_fills_gaps_with_fallback():
    # LLM returns a classification for item 1 only; item 2 gets keyword fallback.
    def client(system, user):
        return {"classifications": [
            {"index": 1, "direction": "bullish", "magnitude": 5,
             "durability": "long", "one_line_summary": "big"},
        ]}

    items = [{"headline": "Co lands deal"}, {"headline": "Co missed estimates badly"}]
    out = news_classifier.classify_news_items("CO", items, llm_client=client)
    assert out[0]["source"] == "llm"
    assert out[1]["source"] == "keyword_fallback"
    assert out[1]["direction"] == "bearish"
