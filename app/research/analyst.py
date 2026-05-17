"""Per-ticker research synthesis.

Pipeline:
  1. Pull quote, technicals, news, earnings date, analyst consensus.
  2. Compute a rule-based signal (always available, no LLM required).
  3. Refine with LLM (GitHub Models) using the wealth-advisor persona.
"""
import json

from app.config import risk_profile
from app.data import calendar, news as news_mod, prices
from app.research import llm, prompts, rules


def gather(ticker: str, position_context: dict | None = None) -> dict:
    """Pull everything we know about a ticker into one dict."""
    q = prices.quote(ticker).to_dict()
    tech = prices.technicals(ticker)
    news = news_mod.company_news(ticker, limit=12)
    earnings = calendar.earnings_date(ticker)
    consensus = calendar.consensus(ticker)
    recs = calendar.analyst_recs(ticker, limit=5)
    return {
        "ticker": ticker,
        "quote": q,
        "technicals": tech,
        "news": news,
        "earnings": earnings,
        "consensus": consensus,
        "analyst_recs": recs,
        "position": position_context or {},
    }


def analyze_ticker(ticker: str, position_context: dict | None = None) -> dict:
    bundle = gather(ticker, position_context)
    risk = risk_profile()

    base = rules.recommend(
        ticker=ticker,
        quote=bundle["quote"],
        tech=bundle["technicals"],
        position_ctx=bundle["position"],
        risk=risk,
        news_count=len(bundle["news"]),
    )

    if llm.available():
        user_blob = (
            f"INVESTOR RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
            f"TICKER: {ticker}\n"
            f"POSITION: {json.dumps(bundle['position'])}\n"
            f"QUOTE: {json.dumps(bundle['quote'])}\n"
            f"TECHNICALS: {json.dumps(bundle['technicals'])}\n"
            f"EARNINGS: {json.dumps(bundle['earnings'])}\n"
            f"ANALYST CONSENSUS: {json.dumps(bundle['consensus'])}\n"
            f"RECENT ANALYST ACTIONS:\n{json.dumps(bundle['analyst_recs'], indent=2)}\n"
            f"RULE-BASED FIRST READ: {json.dumps(base)}\n\n"
            f"RECENT HEADLINES:\n" + "\n".join(f"- {n['headline']}" for n in bundle["news"][:12]) + "\n\n"
            "Write the recommendation JSON now. Specific levels and dates."
        )
        refined = llm.chat_json(prompts.SYSTEM_ANALYST, user_blob, model=llm.routine_model(),
                                max_tokens=900)
        if refined and "action" in refined and "thesis" in refined:
            refined["source"] = "llm"
            base = {**base, **refined}

    return {
        **base,
        "ticker": ticker,
        "quote": bundle["quote"],
        "technicals": bundle["technicals"],
        "news": bundle["news"],
        "earnings": bundle["earnings"],
        "consensus": bundle["consensus"],
        "analyst_recs": bundle["analyst_recs"],
        "position": bundle["position"],
    }
