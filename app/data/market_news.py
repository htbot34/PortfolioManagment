"""Top financial-news headlines via free RSS feeds.

Aggregates CNBC, Yahoo Finance, MarketWatch, and Seeking Alpha (front pages
only). Dedupes by headline.
"""
import feedparser
import httpx

_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC markets", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    ("Reuters business", "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"),
]

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PortfolioAdvisor/1.0)"}
_TIMEOUT = 15.0


def top_headlines(limit_per_feed: int = 6, total_limit: int = 30) -> list[dict]:
    out: list[dict] = []
    seen_titles: set[str] = set()
    for source, url in _FEEDS:
        try:
            r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            feed = feedparser.parse(r.text)
        except Exception:
            continue
        for entry in feed.entries[:limit_per_feed]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            key = title.lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            out.append({
                "source": source,
                "title": title,
                "url": entry.get("link"),
                "published": entry.get("published") or entry.get("updated"),
                "summary": (entry.get("summary") or "")[:300],
            })
            if len(out) >= total_limit:
                return out
    return out
