"""Daily advisory brief - the top-of-page synthesis.

Pulls macro snapshot + scanner results + per-ticker analyses + portfolio
context, then asks the synthesis-tier model to produce concrete trade ideas
with entry/stop/target/size and urgency tags in the wealth-advisor voice.
"""
import json
from datetime import date

from app.config import risk_profile
from app.research import llm, prompts


def _condensed_position(rec: dict) -> dict:
    q = rec.get("quote") or {}
    p = rec.get("position") or {}
    t = rec.get("technicals") or {}
    e = rec.get("earnings") or {}
    c = rec.get("consensus") or {}
    return {
        "ticker": rec["ticker"],
        "price": q.get("price"),
        "day_pct": q.get("day_change_pct"),
        "weight_pct": p.get("weight_pct"),
        "unrealized_pl_pct": p.get("unrealized_pl_pct"),
        "rsi14": t.get("rsi14"),
        "macd_hist": t.get("macd_hist"),
        "bb_pct": t.get("bb_pct"),
        "atr_pct": t.get("atr_pct"),
        "pct_off_52w_high": t.get("pct_off_52w_high"),
        "stacked_uptrend": t.get("stacked_uptrend"),
        "stacked_downtrend": t.get("stacked_downtrend"),
        "earnings_date": e.get("date"),
        "earnings_days_away": e.get("days_away"),
        "analyst_target_mean": c.get("target_mean"),
        "rule_action": rec.get("action"),
        "rule_thesis": rec.get("thesis"),
        "top_news": [n["headline"] for n in (rec.get("news") or [])[:3]],
    }


def _macro_summary(macro: dict) -> dict:
    return {
        "indices": {k: {"price": v.get("price"), "day_pct": v.get("day_change_pct")}
                    for k, v in macro["indices"].items()},
        "leaders": [{"name": s["name"], "day_pct": s["day_change_pct"]} for s in macro["leaders"]],
        "laggards": [{"name": s["name"], "day_pct": s["day_change_pct"]} for s in macro["laggards"]],
    }


def _scanner_condensed(scan_result: dict) -> dict:
    """Trim the scanner output to what's useful in the prompt."""
    def slim(rows):
        return [
            {
                "ticker": r["ticker"], "theme": r.get("theme"),
                "price": r.get("price"), "day_pct": r.get("day_change_pct"),
                "rsi14": r.get("rsi14"), "macd_hist": r.get("macd_hist"),
                "atr_pct": r.get("atr_pct"), "vol_ratio": r.get("vol_ratio_20d"),
                "pct_off_52w_high": r.get("pct_off_52w_high"),
                "stacked_uptrend": r.get("stacked_uptrend"),
                "held": r.get("held"),
            }
            for r in rows
        ]
    return {
        "universe_size": scan_result.get("universe_size"),
        "buckets": {name: slim(rows) for name, rows in scan_result.get("buckets", {}).items()},
        "top_up": slim(scan_result.get("top_movers_up", []))[:5],
        "top_down": slim(scan_result.get("top_movers_down", []))[:5],
    }


def _fallback(recommendations: list[dict], scan_result: dict, review: dict) -> dict:
    """Rule-based fallback when the LLM is unavailable."""
    trade_ideas: list[dict] = []
    # Defense: trim oversized / overbought held positions
    for r in recommendations:
        if r.get("action") in {"trim", "sell"} and r.get("conviction", 0) >= 3:
            trade_ideas.append({
                "ticker": r["ticker"],
                "action": r["action"],
                "setup": "trim_overweight" if "weight" in (r.get("thesis", "").lower()) else "exit",
                "entry": "",
                "stop": "",
                "target_1": "",
                "target_2": None,
                "size_pct": None,
                "urgency": "this_week" if r["action"] == "trim" else "today",
                "horizon": r.get("horizon", "long_term"),
                "thesis": r.get("thesis", ""),
                "invalidation": "",
            })
    # Offense: take top scanner picks
    for bucket_name in ("breakouts", "momentum_continuation", "pullbacks_to_support", "oversold_bounces"):
        for s in scan_result["buckets"].get(bucket_name, [])[:2]:
            if s.get("held"):
                continue
            trade_ideas.append({
                "ticker": s["ticker"],
                "action": "buy",
                "setup": bucket_name.rstrip("s"),
                "entry": f"~${s['price']:.2f}" if s.get("price") else "",
                "stop": f"${s['price'] * 0.93:.2f}" if s.get("price") else "",
                "target_1": f"${s['price'] * 1.10:.2f}" if s.get("price") else "",
                "target_2": f"${s['price'] * 1.20:.2f}" if s.get("price") else None,
                "size_pct": 3,
                "urgency": "this_week",
                "horizon": "swing",
                "thesis": f"{bucket_name.replace('_',' ')} setup; RSI {s.get('rsi14'):.0f}." if s.get("rsi14") else "",
                "invalidation": f"Break below ${s['price'] * 0.93:.2f}" if s.get("price") else "",
            })
    return {
        "headline": "LLM unavailable - rule-based trade ideas from scanner. Configure GITHUB_TOKEN for advisor commentary.",
        "market_pulse": "",
        "trade_ideas": trade_ideas,
        "portfolio_notes": review.get("observations", []),
        "catalysts_this_week": [
            {"ticker": r["ticker"], "event": "earnings",
             "date": (r.get("earnings") or {}).get("date"), "note": ""}
            for r in recommendations
            if (r.get("earnings") or {}).get("days_away") is not None
            and (r["earnings"]["days_away"] is not None and r["earnings"]["days_away"] <= 10)
        ],
    }


def build(macro: dict, recommendations: list[dict], review: dict,
          candidates: dict, exposures: dict, scan_result: dict) -> dict:
    risk = risk_profile()

    if not llm.available():
        out = _fallback(recommendations, scan_result, review)
        out["generated_for"] = date.today().isoformat()
        return out

    payload = {
        "today": date.today().isoformat(),
        "investor_risk_profile": risk,
        "macro": _macro_summary(macro),
        "exposures": {
            "portfolio_value": exposures.get("portfolio_value"),
            "cash": exposures.get("cash"),
            "cash_pct": exposures.get("cash_pct"),
            "sector_pct": exposures.get("sector_pct"),
        },
        "positions": [_condensed_position(r) for r in recommendations],
        "scanner": _scanner_condensed(scan_result),
        "rule_based_review": review,
        "candidate_universe_picks": candidates.get("candidates", []),
    }

    user_blob = (
        "Below is every signal you have for today's call. The scanner has "
        "already done the mechanical work. Your job: cherry-pick the best "
        "setups, give the client concrete trades with entries/stops/targets/size, "
        "lead with defense on the existing book, then offense.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Write the morning brief JSON now. 5-10 trade ideas. Specific levels. Stops on every trade."
    )

    out = llm.chat_json(
        prompts.SYSTEM_DAILY_BRIEF, user_blob,
        model=llm.synthesis_model(),
        max_tokens=3500,
        temperature=0.35,
    )
    if not out or "trade_ideas" not in out:
        out = _fallback(recommendations, scan_result, review)
    out["generated_for"] = date.today().isoformat()
    return out
