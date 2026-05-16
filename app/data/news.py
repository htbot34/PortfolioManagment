"""Free news: yfinance (Yahoo) + Google News RSS. No API keys required."""
from datetime import datetime
from urllib.parse import quote_plus

import feedparser
import httpx
import yfinance as yf


def _yahoo_news(ticker: str) -> list[dict]:
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out = []
    for it in items:
        content = it.get("content") or it
        title = content.get("title") or it.get("title")
        if not title:
            continue
        link = (content.get("clickThroughUrl") or {}).get("url") or content.get("canonicalUrl", {}).get("url") or it.get("link")
        provider = (content.get("provider") or {}).get("displayName") or it.get("publisher", "")
        pub = content.get("pubDate") or it.get("providerPublishTime")
        out.append({"headline": title, "url": link, "source": provider, "published": str(pub) if pub else None})
    return out


def _google_news(ticker: str, limit: int = 10) -> list[dict]:
    q = quote_plus(f"{ticker} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        feed = feedparser.parse(r.text)
    except Exception:
        return []
    out = []
    for entry in feed.entries[:limit]:
        out.append({
            "headline": entry.get("title"),
            "url": entry.get("link"),
            "source": (entry.get("source") or {}).get("title", "Google News"),
            "published": entry.get("published"),
        })
    return out


def company_news(ticker: str, limit: int = 20) -> list[dict]:
    """Merge Yahoo and Google News results, dedupe by headline, return up to `limit`."""
    seen: set[str] = set()
    out: list[dict] = []
    for item in _yahoo_news(ticker) + _google_news(ticker, limit=limit):
        key = (item.get("headline") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out
