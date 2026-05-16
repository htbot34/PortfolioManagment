"""News + sentiment via Finnhub. Returns empty list when FINNHUB_API_KEY unset."""
from datetime import date, timedelta

import httpx

from app.config import settings

_BASE = "https://finnhub.io/api/v1"


def company_news(ticker: str, days: int = 30) -> list[dict]:
    if not settings.finnhub_api_key:
        return []
    to_d = date.today()
    from_d = to_d - timedelta(days=days)
    try:
        r = httpx.get(
            f"{_BASE}/company-news",
            params={
                "symbol": ticker.upper(),
                "from": from_d.isoformat(),
                "to": to_d.isoformat(),
                "token": settings.finnhub_api_key,
            },
            timeout=15,
        )
        r.raise_for_status()
        items = r.json() or []
    except Exception:
        return []
    return [
        {
            "datetime": it.get("datetime"),
            "headline": it.get("headline"),
            "source": it.get("source"),
            "summary": it.get("summary"),
            "url": it.get("url"),
        }
        for it in items[:50]
    ]


def news_sentiment(ticker: str) -> dict | None:
    if not settings.finnhub_api_key:
        return None
    try:
        r = httpx.get(
            f"{_BASE}/news-sentiment",
            params={"symbol": ticker.upper(), "token": settings.finnhub_api_key},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None
