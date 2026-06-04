"""Tests for the news denylist + dedupe."""
from unittest.mock import patch

from app.data import news


def test_dedupes_by_normalized_headline():
    yahoo = [{"headline": "META beats earnings!", "url": "u1", "source": "Reuters", "published": None}]
    google = [{"headline": "Meta beats earnings.", "url": "u2", "source": "WSJ", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=(yahoo, True)), \
         patch.object(news, "_google_news", return_value=(google, True)):
        out = news.company_news("META")
    assert len(out) == 1


def test_denylisted_source_dropped_without_ticker():
    items = [{"headline": "Quarterly update from a generic small cap",
              "url": "u", "source": "GlobeNewswire", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=(items, True)), \
         patch.object(news, "_google_news", return_value=([], True)):
        out = news.company_news("META")
    assert out == []


def test_denylisted_source_kept_when_ticker_in_headline():
    items = [{"headline": "META Platforms announces buyback",
              "url": "u", "source": "GlobeNewswire", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=(items, True)), \
         patch.object(news, "_google_news", return_value=([], True)):
        out = news.company_news("META")
    assert len(out) == 1


def test_limit_applied():
    items = [{"headline": f"News story {i}", "url": f"u{i}", "source": "Reuters", "published": None}
             for i in range(40)]
    with patch.object(news, "_yahoo_news", return_value=(items, True)), \
         patch.object(news, "_google_news", return_value=([], True)):
        out = news.company_news("AAPL", limit=10)
    assert len(out) == 10


def test_status_ok_when_items_present():
    items = [{"headline": "META ships a thing", "url": "u", "source": "Reuters", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=(items, True)), \
         patch.object(news, "_google_news", return_value=([], True)):
        out, status = news.company_news_with_status("META")
    assert status == "ok" and len(out) == 1


def test_status_empty_when_feeds_respond_clean_but_no_news():
    with patch.object(news, "_yahoo_news", return_value=([], True)), \
         patch.object(news, "_google_news", return_value=([], True)):
        out, status = news.company_news_with_status("META")
    assert out == [] and status == "empty"


def test_status_outage_when_both_feeds_fail():
    """A both-feeds-down outage MUST be distinguishable from a clean empty."""
    with patch.object(news, "_yahoo_news", return_value=([], False)), \
         patch.object(news, "_google_news", return_value=([], False)):
        out, status = news.company_news_with_status("META")
    assert out == [] and status == "outage"


def test_status_ok_when_one_feed_fails_but_other_has_news():
    items = [{"headline": "META ships a thing", "url": "u", "source": "Reuters", "published": None}]
    with patch.object(news, "_yahoo_news", return_value=([], False)), \
         patch.object(news, "_google_news", return_value=(items, True)):
        out, status = news.company_news_with_status("META")
    assert status == "ok" and len(out) == 1
