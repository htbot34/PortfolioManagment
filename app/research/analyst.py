"""Claude-driven per-ticker research pipeline.

Two-stage:
  1) Haiku summarizes each SEC filing (cached forever on disk by accession).
  2) Opus produces a structured recommendation from {risk_profile, quote,
     technicals, news digest, filing summaries}.

Prompt caching is used for the large/stable inputs (system prompt, filing
summary bundle, risk profile) so repeated runs are cheap.
"""
import json
from pathlib import Path

from app.config import settings, risk_profile
from app.data import filings as edgar
from app.data import news as news_mod
from app.data import prices
from app.research import prompts

_SUMMARY_CACHE = settings.cache_dir / "filing_summaries"
_SUMMARY_CACHE.mkdir(exist_ok=True)


def _client():
    if not settings.anthropic_api_key:
        return None
    from anthropic import Anthropic
    return Anthropic(api_key=settings.anthropic_api_key)


def summarize_filing(filing: dict) -> str:
    """Stage 1: cheap Haiku summary, cached on disk by accession number."""
    cache = _SUMMARY_CACHE / f"{filing['accession']}.md"
    if cache.exists():
        return cache.read_text()
    client = _client()
    if client is None:
        return ""
    text = edgar.fetch_filing_text(filing, max_chars=120_000)
    if not text or text.startswith("[fetch failed"):
        return ""
    msg = client.messages.create(
        model=settings.claude_summary_model,
        max_tokens=900,
        system=prompts.SYSTEM_FILING_SUMMARIZER,
        messages=[{
            "role": "user",
            "content": [{
                "type": "text",
                "text": f"Form: {filing['form']}\nFiled: {filing['filed']}\n\n---\n{text}",
            }],
        }],
    )
    summary = msg.content[0].text if msg.content else ""
    cache.write_text(summary)
    return summary


def analyze_ticker(ticker: str, position_context: dict | None = None) -> dict:
    """Stage 2: produce a recommendation JSON for a single ticker."""
    client = _client()
    q = prices.quote(ticker)
    tech = prices.technicals(ticker)
    news = news_mod.company_news(ticker, days=30)[:15]
    sentiment = news_mod.news_sentiment(ticker)
    filings = edgar.recent_filings(ticker)
    summaries: list[dict] = []
    for f in filings:
        s = summarize_filing(f)
        if s:
            summaries.append({"form": f["form"], "filed": f["filed"], "summary": s})

    if client is None:
        return {
            "ticker": ticker,
            "error": "ANTHROPIC_API_KEY not set",
            "quote": q.to_dict(),
            "technicals": tech,
            "news_count": len(news),
            "filings_count": len(filings),
        }

    risk = risk_profile()

    static_block = (
        f"INVESTOR RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
        f"FILING SUMMARIES:\n{json.dumps(summaries, indent=2)}"
    )
    live_block = (
        f"TICKER: {ticker}\n"
        f"POSITION CONTEXT: {json.dumps(position_context or {})}\n\n"
        f"QUOTE: {json.dumps(q.to_dict())}\n\n"
        f"TECHNICALS: {json.dumps(tech)}\n\n"
        f"NEWS SENTIMENT: {json.dumps(sentiment)}\n\n"
        f"RECENT HEADLINES:\n{json.dumps([n['headline'] for n in news], indent=2)}\n\n"
        "Produce the recommendation JSON now."
    )

    msg = client.messages.create(
        model=settings.claude_research_model,
        max_tokens=1500,
        system=[
            {"type": "text", "text": prompts.SYSTEM_ANALYST, "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": static_block, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": live_block},
            ],
        }],
    )
    raw = msg.content[0].text if msg.content else "{}"
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        rec = {"raw": raw, "error": "model returned non-JSON"}
    rec["ticker"] = ticker
    rec["quote"] = q.to_dict()
    rec["technicals"] = tech
    rec["news_count"] = len(news)
    rec["filings"] = [{"form": f["form"], "filed": f["filed"], "url": f["url"]} for f in filings]
    return rec
