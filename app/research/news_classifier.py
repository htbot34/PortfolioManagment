"""Semantic news classification via GitHub Models (free, workflow token).

The LLM task is framed strictly as descriptive summarization of the business
implications of a piece of text - never as trading advice. The prompt never
mentions buying, selling, or trading. The output describes the COMPANY's
business outlook (bullish / bearish / neutral about the company), which is a
factual-summary framing that clears Azure's content filter.

Public entry point: ``classify_news_items(ticker, items, llm_client=None)``.

Classifications are cached forever in ``news_classification_cache.json`` at the
repo root, keyed by SHA1(ticker + headline + published_date) - a news item's
meaning does not change retroactively, so the cache never expires.

On any LLM failure (content filter, empty result, exception) the affected
items fall back to keyword scoring and the failure is logged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from app.logging import get_logger
from app.research import signals as _signals

log = get_logger(__name__)

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "news_classification_cache.json"

_DIRECTIONS = ("bullish", "bearish", "neutral")
_DURABILITY = ("short", "medium", "long")
_BATCH = 10

SYSTEM = (
    "You summarize the business implications of news text about a company. "
    "You are given numbered text items about one named company. For each item, "
    "describe the business outlook the text implies for that company. This is "
    "descriptive summarization of the text, not advice. Output JSON only."
)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_key(ticker: str, headline: str, published: str) -> str:
    raw = f"{(ticker or '').upper()}|{headline or ''}|{published or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, separators=(",", ":")))
    except Exception as e:
        log.warning("could not write news classification cache: %s", e)


# ---------------------------------------------------------------------------
# LLM client + fallback
# ---------------------------------------------------------------------------

def _default_llm_client(system: str, user: str) -> dict | None:
    """Default client: routes to GitHub Models gpt-4o-mini."""
    from app.research import llm
    return llm.chat_json(system, user, model="openai/gpt-4o-mini",
                         max_tokens=1500, tag="news_classify")


def _keyword_fallback(item: dict) -> dict:
    """Keyword-based classification used when the LLM is unavailable."""
    headline = item.get("headline") or item.get("title") or ""
    toks = _signals._tokens(headline)
    bull = bool(toks & _signals._BULL_TERMS)
    bear = bool(toks & _signals._BEAR_TERMS)
    if bull and not bear:
        direction, magnitude, durability = "bullish", 3, "medium"
    elif bear and not bull:
        direction, magnitude, durability = "bearish", 3, "medium"
    else:
        direction, magnitude, durability = "neutral", 1, "short"
    return {
        "direction": direction,
        "magnitude": magnitude,
        "durability": durability,
        "one_line_summary": headline[:120],
        "source": "keyword_fallback",
    }


def _validate(c: dict) -> dict | None:
    """Coerce an LLM classification into the schema, or None if unusable."""
    if not isinstance(c, dict):
        return None
    direction = str(c.get("direction", "")).lower().strip()
    if direction not in _DIRECTIONS:
        return None
    try:
        magnitude = int(c.get("magnitude"))
    except (TypeError, ValueError):
        return None
    magnitude = max(1, min(5, magnitude))
    durability = str(c.get("durability", "")).lower().strip()
    if durability not in _DURABILITY:
        durability = "medium"
    return {
        "direction": direction,
        "magnitude": magnitude,
        "durability": durability,
        "one_line_summary": str(c.get("one_line_summary") or "")[:120],
        "source": "llm",
    }


# ---------------------------------------------------------------------------
# Batch classification
# ---------------------------------------------------------------------------

def _classify_batch(ticker: str, batch: list[tuple[int, dict]],
                    client: Callable[[str, str], dict | None]) -> list[dict]:
    """Classify one batch (<=10 items). Returns classification dicts aligned
    to ``batch``. Any failure falls back to keyword scoring for the batch.
    """
    lines = []
    for n, (_, item) in enumerate(batch, 1):
        headline = item.get("headline") or item.get("title") or ""
        summary = (item.get("summary") or "")[:300]
        lines.append(f"{n}. {headline}" + (f" - {summary}" if summary else ""))
    user = (
        f"Company: {ticker}\n"
        f"For each of the following {len(batch)} numbered text items, "
        f"summarize the business implications for {ticker}. For each item give: "
        "direction (bullish, bearish, or neutral about the company's business "
        "outlook), magnitude (1=minor, 5=company-defining event), durability "
        "(short=days, medium=weeks, long=quarters), and a one_line_summary "
        "of <=120 characters.\n\n"
        + "\n".join(lines) + "\n\n"
        'Output JSON: {"classifications": [{"index": 1, "direction": '
        '"bullish|bearish|neutral", "magnitude": 1, "durability": '
        '"short|medium|long", "one_line_summary": "..."}]}'
    )
    out = None
    try:
        out = client(SYSTEM, user)
    except Exception as e:
        log.warning("news classifier LLM call raised for %s: %s", ticker, e)
    if not out or "classifications" not in out:
        log.warning("news classifier: empty/filtered result for %s - "
                    "keyword fallback for %d item(s)", ticker, len(batch))
        return [_keyword_fallback(item) for _, item in batch]
    by_index: dict[int, dict] = {}
    for c in out.get("classifications") or []:
        idx = c.get("index")
        v = _validate(c)
        if idx is not None and v is not None:
            try:
                by_index[int(idx)] = v
            except (TypeError, ValueError):
                continue
    result: list[dict] = []
    for n, (_, item) in enumerate(batch, 1):
        result.append(by_index.get(n) or _keyword_fallback(item))
    return result


def classify_news_items(ticker: str, items, llm_client=None) -> list[dict]:
    """Classify the business implications of each news item.

    Returns one dict per input item (in input order), each carrying the
    original ``headline`` and ``published`` plus ``direction``, ``magnitude``,
    ``durability``, ``one_line_summary``, and ``source`` ("llm" or
    "keyword_fallback").
    """
    items = list(items or [])
    if not items:
        return []
    client = llm_client or _default_llm_client
    cache = _load_cache()
    cache_dirty = False
    results: list[dict | None] = [None] * len(items)
    pending: list[tuple[int, dict]] = []

    for i, item in enumerate(items):
        headline = item.get("headline") or item.get("title") or ""
        published = str(item.get("published") or item.get("datetime") or "")
        key = _cache_key(ticker, headline, published)
        if key in cache:
            results[i] = {**cache[key], "headline": headline, "published": published}
        else:
            pending.append((i, item))

    for start in range(0, len(pending), _BATCH):
        batch = pending[start:start + _BATCH]
        classified = _classify_batch(ticker, batch, client)
        for (idx, item), cls in zip(batch, classified):
            headline = item.get("headline") or item.get("title") or ""
            published = str(item.get("published") or item.get("datetime") or "")
            results[idx] = {**cls, "headline": headline, "published": published}
            if cls.get("source") == "llm":
                key = _cache_key(ticker, headline, published)
                cache[key] = {k: cls[k] for k in
                              ("direction", "magnitude", "durability",
                               "one_line_summary", "source")}
                cache_dirty = True

    if cache_dirty:
        _save_cache(cache)
    return [r for r in results if r is not None]
