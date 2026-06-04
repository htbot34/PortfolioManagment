"""Free news: yfinance (Yahoo) + Google News RSS. No API keys required.

Quality filter:
- Dedupe by normalized headline (lowercased, non-alphanumeric stripped) so the
  same story syndicated across feeds collapses to one entry.
- Drop press-release wire services unless the headline explicitly mentions the
  ticker symbol (they tend to be sponsored noise without it).
- Cap at ``limit`` after filtering.
"""
import re
from urllib.parse import quote_plus

import feedparser
import httpx
import yfinance as yf

from app.logging import get_logger

log = get_logger(__name__)

# PR-wire / press-release feeds. Allowed through only if the ticker is in the
# headline (then it's at least a company-issued release for THIS company).
_DENYLIST = {"globenewswire", "pr newswire", "business wire", "accesswire",
             "newsfile", "newsfile corp.", "businesswire", "prnewswire"}

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", (s or "").lower())


def _is_denylisted(source: str, headline: str, ticker: str) -> bool:
    if not source:
        return False
    if source.strip().lower() not in _DENYLIST:
        return False
    return ticker.upper() not in (headline or "").upper()


def _yahoo_news(ticker: str) -> tuple[list[dict], bool]:
    """Return ``(items, ok)``. ``ok`` is False on a genuine fetch failure -
    distinct from a clean empty result - so a both-feeds-down outage can be
    told apart from "this company simply has no recent news"."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception as e:
        log.debug("yahoo .news failed for %s: %s", ticker, e)
        return [], False
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
    return out, True


def _google_news(ticker: str, limit: int = 10) -> tuple[list[dict], bool]:
    """Return ``(items, ok)``. See :func:`_yahoo_news` for ``ok`` semantics."""
    q = quote_plus(f"{ticker} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        feed = feedparser.parse(r.text)
    except Exception as e:
        log.debug("google news failed for %s: %s", ticker, e)
        return [], False
    out = []
    for entry in feed.entries[:limit]:
        out.append({
            "headline": entry.get("title"),
            "url": entry.get("link"),
            "source": (entry.get("source") or {}).get("title", "Google News"),
            "published": entry.get("published"),
        })
    return out, True


def _merge_feeds(ticker: str, limit: int) -> tuple[list[dict], str]:
    """Merge Yahoo + Google, denylist PR wires, dedupe, cap at ``limit``, and
    classify the fetch outcome.

    Status:
      ``ok``      at least one merged item survived filtering.
      ``empty``   at least one feed responded cleanly but there was no news.
      ``outage``  BOTH feeds errored - no provider responded. This is a data
                  outage, NOT genuinely-no-news, and the caller must be able to
                  tell them apart (otherwise a feed failure silently fails the
                  conviction gate's news signal closed).
    """
    yahoo, y_ok = _yahoo_news(ticker)
    google, g_ok = _google_news(ticker, limit=limit)
    seen: set[str] = set()
    out: list[dict] = []
    for item in yahoo + google:
        headline = (item.get("headline") or "").strip()
        if not headline:
            continue
        key = _norm(headline)
        if not key or key in seen:
            continue
        if _is_denylisted(item.get("source") or "", headline, ticker):
            log.debug("dropped %s for %s (PR wire without ticker mention)",
                       item.get("source"), ticker)
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    if out:
        status = "ok"
    elif not y_ok and not g_ok:
        status = "outage"
    else:
        status = "empty"
    return out, status


def company_news(ticker: str, limit: int = 20) -> list[dict]:
    """Merge Yahoo + Google News, denylist PR wires, dedupe, cap at limit."""
    return _merge_feeds(ticker, limit)[0]


def company_news_with_status(ticker: str, limit: int = 20) -> tuple[list[dict], str]:
    """Like :func:`company_news` but also returns a fetch status in
    ``{ok, empty, outage}`` so a both-feeds-down outage is observable rather
    than coalesced into an empty list. Used by the conviction gate so news
    outages are recorded distinctly in the durable telemetry."""
    return _merge_feeds(ticker, limit)
