"""Per-ticker recommender. Combines rule-based signals with optional LLM commentary.

Rules always run and provide the action/horizon/conviction. The LLM (GitHub
Models, free) is asked for a richer thesis and additional catalysts/risks; if
unavailable or rate-limited, the rule output is returned as-is.
"""
import json

from app.config import risk_profile
from app.data import news as news_mod
from app.data import prices
from app.research import llm, prompts, rules


def analyze_ticker(ticker: str, position_context: dict | None = None) -> dict:
    q = prices.quote(ticker).to_dict()
    tech = prices.technicals(ticker)
    news = news_mod.company_news(ticker, limit=12)
    risk = risk_profile()

    base = rules.recommend(
        ticker=ticker,
        quote=q,
        tech=tech,
        position_ctx=position_context or {},
        risk=risk,
        news_count=len(news),
    )

    if llm.available():
        user_blob = (
            f"INVESTOR RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
            f"TICKER: {ticker}\n"
            f"POSITION CONTEXT: {json.dumps(position_context or {})}\n"
            f"QUOTE: {json.dumps(q)}\n"
            f"TECHNICALS: {json.dumps(tech)}\n"
            f"RULE-BASED SIGNAL: {json.dumps(base)}\n\n"
            f"RECENT HEADLINES:\n" + "\n".join(f"- {n['headline']}" for n in news[:12]) + "\n\n"
            "Refine the recommendation. You may override the rule-based action if the news warrants it. "
            "Output the JSON now."
        )
        refined = llm.chat_json(prompts.SYSTEM_ANALYST, user_blob, max_tokens=900)
        if refined and "action" in refined and "thesis" in refined:
            refined["source"] = "llm"
            base = {**base, **refined}

    return {
        "ticker": ticker,
        **base,
        "quote": q,
        "technicals": tech,
        "news": news,
        "position": position_context or {},
    }
