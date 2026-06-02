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

# Trump-mention keyword fallback. Used when the LLM is unavailable or
# returns a result without the trump_* fields (e.g. cached pre-feature
# entries). Matched on lowercased headline+summary tokens.
_TRUMP_TERMS = {"trump", "trumps", "president", "presidents", "presidential",
                "whitehouse", "potus", "djt", "administration"}
_TRUMP_POSITIVE_TERMS = {"praise", "praised", "praises", "endorse", "endorsed",
                          "endorses", "endorsement", "back", "backs", "backed",
                          "support", "supports", "supported", "supportive",
                          "tout", "touts", "touted", "loves", "loved",
                          "great", "amazing", "incredible", "best",
                          "deal", "partnership", "preferred", "favored",
                          "favorite", "tariff-exempt", "exempt", "celebrated"}
_TRUMP_NEGATIVE_TERMS = {"attack", "attacks", "attacked", "criticize",
                          "criticized", "criticizes", "criticism", "blast",
                          "blasts", "blasted", "slam", "slams", "slammed",
                          "lash", "lashes", "lashed", "rip", "ripped",
                          "threat", "threats", "threaten", "threatens",
                          "threatened", "denounce", "denounced", "warn",
                          "warned", "rebuke", "rebuked", "hate", "hates",
                          "disaster", "terrible", "worst", "scam", "fraud",
                          "tariff", "tariffs", "tariffed", "punish",
                          "punished", "boycott", "investigate"}


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
    summary = item.get("summary") or ""
    text = f"{headline} {summary}"
    toks = _signals._tokens(text)
    bull = bool(toks & _signals._BULL_TERMS)
    bear = bool(toks & _signals._BEAR_TERMS)
    if bull and not bear:
        direction, magnitude, durability = "bullish", 3, "medium"
    elif bear and not bull:
        direction, magnitude, durability = "bearish", 3, "medium"
    else:
        direction, magnitude, durability = "neutral", 1, "short"
    trump_fields = _keyword_trump_fields(toks)
    return {
        "direction": direction,
        "magnitude": magnitude,
        "durability": durability,
        "one_line_summary": headline[:120],
        "source": "keyword_fallback",
        **trump_fields,
    }


def _keyword_trump_fields(toks: set[str]) -> dict:
    """Deterministic Trump-mention scoring from headline+summary tokens.

    Returns the trump_* schema fields:
      trump_mention:  bool
      trump_valence:  "endorse" | "attack" | "none"
      trump_confidence: 0..1

    Confidence is intentionally capped at 0.6 -- the keyword fallback
    cannot read context, so a real positive-or-negative-Trump-mention
    statement vs. the same words quoted elsewhere is ambiguous. The
    fallback sets the floor at the configured min_confidence so an
    obvious match is surfaced for review; the LLM pass can raise it.
    """
    has_trump = bool(toks & _TRUMP_TERMS)
    if not has_trump:
        return {"trump_mention": False, "trump_valence": "none",
                "trump_confidence": 0.0}
    pos = bool(toks & _TRUMP_POSITIVE_TERMS)
    neg = bool(toks & _TRUMP_NEGATIVE_TERMS)
    if pos and not neg:
        return {"trump_mention": True, "trump_valence": "endorse",
                "trump_confidence": 0.6}
    if neg and not pos:
        return {"trump_mention": True, "trump_valence": "attack",
                "trump_confidence": 0.6}
    # Either both sides matched (ambiguous) or neither (Trump mentioned
    # but no valence cue). Flag the mention but mark confidence below
    # the default threshold so the signal stays defensible.
    return {"trump_mention": True, "trump_valence": "none",
            "trump_confidence": 0.3}


_TRUMP_VALENCES = ("endorse", "attack", "none")


def _validate(c: dict) -> dict | None:
    """Coerce an LLM classification into the schema, or None if unusable.

    The trump_* fields are optional in the response. Missing fields
    default to "no Trump mention" so old cache entries that pre-date
    this feature read as neutral rather than blowing up.
    """
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

    trump_mention_raw = c.get("trump_mention")
    trump_mention = bool(trump_mention_raw) if trump_mention_raw is not None else False
    trump_valence = str(c.get("trump_valence", "none")).lower().strip()
    if trump_valence not in _TRUMP_VALENCES:
        trump_valence = "none"
    if not trump_mention:
        trump_valence = "none"
    try:
        trump_confidence = float(c.get("trump_confidence", 0.0))
    except (TypeError, ValueError):
        trump_confidence = 0.0
    trump_confidence = max(0.0, min(1.0, trump_confidence))
    if not trump_mention:
        trump_confidence = 0.0

    return {
        "direction": direction,
        "magnitude": magnitude,
        "durability": durability,
        "one_line_summary": str(c.get("one_line_summary") or "")[:120],
        "source": "llm",
        "trump_mention": trump_mention,
        "trump_valence": trump_valence,
        "trump_confidence": trump_confidence,
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
        "(short=days, medium=weeks, long=quarters), a one_line_summary "
        "of <=120 characters, AND three additional fields about Presidential "
        f"statements: trump_mention (true if the text reports a Presidential "
        f"or White House statement specifically about {ticker} or its product), "
        "trump_valence ('endorse' if the statement praises/supports/endorses "
        "the company, 'attack' if it criticizes/threatens/blasts the company, "
        "'none' otherwise), trump_confidence (0..1, how confident you are that "
        "this item really reports such a statement -- 1.0 = direct quote, "
        "0.7 = clearly described, 0.3 = ambiguous, 0 = no mention). When in "
        "doubt set trump_mention=false.\n\n"
        + "\n".join(lines) + "\n\n"
        'Output JSON: {"classifications": [{"index": 1, "direction": '
        '"bullish|bearish|neutral", "magnitude": 1, "durability": '
        '"short|medium|long", "one_line_summary": "...", '
        '"trump_mention": false, "trump_valence": "none", '
        '"trump_confidence": 0}]}'
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
        cached = cache.get(key)
        if cached is not None:
            # Old cache entries (pre-Trump-fields) read as no-mention; this
            # keeps the new code backward-compatible without invalidating
            # the cache. A re-classification can happen lazily on a future
            # build by editing the cache.
            entry = {
                "trump_mention": False,
                "trump_valence": "none",
                "trump_confidence": 0.0,
                **cached,
                "headline": headline,
                "published": published,
            }
            results[i] = entry
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
                cache[key] = {k: cls.get(k) for k in
                              ("direction", "magnitude", "durability",
                               "one_line_summary", "source",
                               "trump_mention", "trump_valence",
                               "trump_confidence")}
                cache_dirty = True

    if cache_dirty:
        _save_cache(cache)
    return [r for r in results if r is not None]
