"""Daily advisory brief - the top-of-page synthesis.

Pulls together macro snapshot, per-ticker analyses, portfolio review, and
candidate ideas, then asks the highest-tier model to write the morning note
in the wealth-advisor voice.
"""
import json
from datetime import date

from app.config import risk_profile
from app.research import llm, prompts


def _condensed_position(rec: dict) -> dict:
    """Keep the brief prompt tight - one line per position."""
    q = rec.get("quote") or {}
    p = rec.get("position") or {}
    earnings = rec.get("earnings") or {}
    consensus = rec.get("consensus") or {}
    return {
        "ticker": rec["ticker"],
        "price": q.get("price"),
        "day_change_pct": q.get("day_change_pct"),
        "weight_pct": p.get("weight_pct"),
        "unrealized_pl_pct": p.get("unrealized_pl_pct"),
        "rsi14": (rec.get("technicals") or {}).get("rsi14"),
        "pct_off_52w_high": (rec.get("technicals") or {}).get("pct_off_52w_high"),
        "rule_action": rec.get("action"),
        "rule_thesis": rec.get("thesis"),
        "rule_conviction": rec.get("conviction"),
        "earnings_date": earnings.get("date"),
        "earnings_days_away": earnings.get("days_away"),
        "analyst_target_mean": consensus.get("target_mean"),
        "analyst_recommendation": consensus.get("recommendation"),
        "top_news": [n["headline"] for n in (rec.get("news") or [])[:5]],
    }


def _macro_summary(macro: dict) -> dict:
    """Strip macro snapshot down to essentials for the prompt."""
    return {
        "indices": {k: {"price": v["price"], "day_pct": v["day_change_pct"]}
                    for k, v in macro["indices"].items()},
        "leaders": [{"name": s["name"], "day_pct": s["day_change_pct"]} for s in macro["leaders"]],
        "laggards": [{"name": s["name"], "day_pct": s["day_change_pct"]} for s in macro["laggards"]],
    }


def build(macro: dict, recommendations: list[dict], review: dict,
          candidates: dict, exposures: dict) -> dict:
    """Return the brief payload. If LLM is unavailable, return a stub."""
    risk = risk_profile()
    fallback = {
        "generated_for": date.today().isoformat(),
        "headline_call": "LLM unavailable - see recommendations and portfolio review for rule-based output.",
        "market_context": "",
        "actions": [
            {
                "ticker": r["ticker"], "action": r["action"], "target": r.get("suggested_action_detail", ""),
                "urgency": "this_week" if r.get("conviction", 2) >= 4 else "patient",
                "rationale": r.get("thesis", ""),
            }
            for r in sorted(recommendations,
                            key=lambda x: (x.get("action") not in {"sell", "trim"},
                                           -(x.get("conviction") or 0)))
        ],
        "portfolio_health": "; ".join(review.get("observations") or []),
        "upcoming_catalysts": [
            {"ticker": r["ticker"], "event": "Earnings",
             "date": (r.get("earnings") or {}).get("date")}
            for r in recommendations
            if (r.get("earnings") or {}).get("date")
        ],
        "outside_ideas": candidates.get("candidates", [])[:3],
    }

    if not llm.available():
        return fallback

    payload = {
        "today": date.today().isoformat(),
        "investor_risk_profile": risk,
        "macro": _macro_summary(macro),
        "exposures": {
            "portfolio_value": exposures.get("portfolio_value"),
            "cash_pct": exposures.get("cash_pct"),
            "sector_pct": exposures.get("sector_pct"),
        },
        "positions": [_condensed_position(r) for r in recommendations],
        "rule_based_review": review,
        "outside_ideas_available": candidates.get("candidates", []),
    }

    user_blob = (
        "Here is everything you need to write today's note.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Now write the morning brief JSON. Be specific. Lead with the most "
        "important action. Reference levels and dates. Defense first."
    )

    out = llm.chat_json(
        prompts.SYSTEM_DAILY_BRIEF, user_blob,
        model=llm.synthesis_model(),
        max_tokens=2500,
        temperature=0.4,
    )
    if not out or "headline_call" not in out:
        return fallback

    out["generated_for"] = date.today().isoformat()
    return out
