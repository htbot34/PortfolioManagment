"""Tests for the news denylist + dedupe."""
from unittest.mock import patch

from app.data import news


def test_dedupes_by_normalized_headline():
    yahoo = [{"headline": "META beats earnings!", "url": "u1", "source": "Reuters", "published": None}]
    google = [{"headline": "Meta beats earnings.", "url": "u2", "source": "WSJ", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=yahoo), \
         patch.object(news, "_google_news", return_value=google):
        out = news.company_news("META")
    assert len(out) == 1


def test_denylisted_source_dropped_without_ticker():
    items = [{"headline": "Quarterly update from a generic small cap",
              "url": "u", "source": "GlobeNewswire", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=items), \
         patch.object(news, "_google_news", return_value=[]):
        out = news.company_news("META")
    assert out == []


def test_denylisted_source_kept_when_ticker_in_headline():
    items = [{"headline": "META Platforms announces buyback",
              "url": "u", "source": "GlobeNewswire", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=items), \
         patch.object(news, "_google_news", return_value=[]):
        out = news.company_news("META")
    assert len(out) == 1


def test_limit_applied():
    items = [{"headline": f"News story {i}", "url": f"u{i}", "source": "Reuters", "published": None}
             for i in range(40)]
    with patch.object(news, "_yahoo_news", return_value=items), \
         patch.object(news, "_google_news", return_value=[]):
        out = news.company_news("AAPL", limit=10)
    assert len(out) == 10
