"""Daily advisory brief - the top-of-page synthesis.

Pulls macro snapshot + scanner results + per-ticker analyses + portfolio
context, then asks the synthesis-tier model to produce concrete trade ideas
with entry/stop/target/size and urgency tags in the wealth-advisor voice.
"""
import json
from datetime import date

from app.config import risk_profile
from app.data import market_news
from app.research import llm, prompts


def _round(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else v


def _condensed_position(rec: dict) -> dict:
    q = rec.get("quote") or {}
    p = rec.get("position") or {}
    t = rec.get("technicals") or {}
    e = rec.get("earnings") or {}
    c = rec.get("consensus") or {}
    f4 = rec.get("insider_form4") or {}
    soc = rec.get("social_attention") or {}
    out = {
        "tk": rec["ticker"],
        "px": _round(q.get("price")),
        "d%": _round(q.get("day_change_pct"), 2),
        "wt%": _round(p.get("weight_pct"), 1),
        "pl%": _round(p.get("unrealized_pl_pct"), 1),
        "rsi": _round(t.get("rsi14"), 0),
        "macd": _round(t.get("macd_hist"), 2),
        "atr%": _round(t.get("atr_pct"), 1),
        "off52": _round(t.get("pct_off_52w_high"), 1),
        "up": t.get("stacked_uptrend"),
        "dn": t.get("stacked_downtrend"),
        "er": e.get("date"),
        "erDay": e.get("days_away"),
        "tgt": _round(c.get("target_mean")),
        "f4": f4.get("count"),
        "wsb": soc.get("post_count_7d"),
        "rule": rec.get("action"),
        "rThesis": (rec.get("thesis") or "")[:200],
    }
    return {k: v for k, v in out.items() if v not in (None, "", False)}


def _macro_summary(macro: dict) -> dict:
    return {
        "idx": {k: _round(v.get("day_change_pct"), 2) for k, v in macro["indices"].items()},
        "lead": [f"{s['name']} {_round(s['day_change_pct'],1)}%" for s in macro["leaders"]],
        "lag": [f"{s['name']} {_round(s['day_change_pct'],1)}%" for s in macro["laggards"]],
    }


def _scanner_condensed(scan_result: dict) -> dict:
    """Hyper-compact scanner output: ticker + price + key signal only."""
    def slim(rows, n=4):
        out = []
        for r in rows[:n]:
            entry = {"tk": r["ticker"], "px": _round(r.get("price"))}
            if r.get("rsi14") is not None:
                entry["rsi"] = _round(r["rsi14"], 0)
            if r.get("vol_ratio_20d"):
                entry["vol"] = _round(r["vol_ratio_20d"], 1)
            if r.get("pct_off_52w_high") is not None:
                entry["off52"] = _round(r["pct_off_52w_high"], 0)
            if r.get("held"):
                entry["h"] = True
            out.append(entry)
        return out
    keep_buckets = ("breakouts", "momentum_continuation", "oversold_bounces",
                    "pullbacks_to_support", "new_52w_highs")
    return {
        "size": scan_result.get("universe_size"),
        "buckets": {name: slim(scan_result["buckets"].get(name, []), 3) for name in keep_buckets
                    if scan_result["buckets"].get(name)},
        "up": slim(scan_result.get("top_movers_up", []), 4),
        "down": slim(scan_result.get("top_movers_down", []), 4),
    }


def _fmt(x: float | None) -> str:
    return f"${x:,.2f}" if x is not None else ""


def _exit_trade(r: dict) -> dict:
    """Build trade idea for an exit (trim/sell) using technicals for stop/target."""
    q = r.get("quote") or {}
    t = r.get("technicals") or {}
    price = q.get("price")
    atr = t.get("atr14")
    sma50 = t.get("sma50")
    sma200 = t.get("sma200")
    is_trim = r["action"] == "trim"
    # Exit at current price or on bounce; stop is structural support
    exit_price = price
    bounce_zone = None
    if price and sma50 and price < sma50:
        bounce_zone = sma50  # bounce to SMA50 = exit zone
    elif price and sma200 and price < sma200:
        bounce_zone = sma200
    entry_str = (f"Trim 25-50% now at ~{_fmt(price)}" if is_trim
                 else (f"Sell into bounce toward {_fmt(bounce_zone)}" if bounce_zone
                       else f"Sell at ~{_fmt(price)}"))
    return {
        "ticker": r["ticker"],
        "action": r["action"],
        "setup": "trim_overweight" if "weight" in (r.get("thesis", "").lower()) else "exit",
        "entry": entry_str,
        "stop": _fmt(price + atr) if (price and atr and is_trim) else "n/a (closing position)",
        "target_1": _fmt(sma200) if (sma200 and price and price > sma200) else (_fmt(bounce_zone) if bounce_zone else "exit at market"),
        "target_2": None,
        "size_pct": None,
        "urgency": "this_week" if is_trim else "today",
        "horizon": r.get("horizon", "long_term"),
        "thesis": r.get("thesis", ""),
        "invalidation": f"Hold of {_fmt(sma200)} on volume" if sma200 else "",
    }


def _entry_trade(s: dict, kind: str) -> dict:
    """Build trade idea for a new buy from scanner data."""
    price = s.get("price")
    sma50 = s.get("sma50")
    atr = s.get("atr14")
    bb_lower = s.get("bb_lower")
    if not price:
        return {}
    # Entry depends on setup
    if kind == "breakouts":
        entry = f"On confirmed close above {_fmt(price)}"
        stop = _fmt(price - 1.5 * atr) if atr else _fmt(price * 0.94)
    elif kind == "momentum_continuation":
        entry = f"Buy on pullback to {_fmt(sma50)}-{_fmt(price)}" if sma50 else f"~{_fmt(price)}"
        stop = _fmt(price - 1.5 * atr) if atr else _fmt(price * 0.93)
    elif kind == "oversold_bounces":
        entry = f"~{_fmt(price)} (oversold)"
        stop = _fmt(bb_lower * 0.97) if bb_lower else _fmt(price * 0.92)
    elif kind == "pullbacks_to_support":
        entry = f"~{_fmt(price)} at SMA50 support {_fmt(sma50)}"
        stop = _fmt(price - 1.5 * atr) if atr else _fmt(price * 0.94)
    else:
        entry = f"~{_fmt(price)}"
        stop = _fmt(price - 1.5 * atr) if atr else _fmt(price * 0.93)
    target_1 = _fmt(price * 1.10)
    target_2 = _fmt(price * 1.20)
    rsi = s.get("rsi14")
    rsi_note = f"RSI {rsi:.0f}" if rsi else "RSI unknown"
    vol = s.get("vol_ratio_20d")
    vol_note = f"volume {vol:.1f}x 20d avg" if vol else ""
    return {
        "ticker": s["ticker"],
        "action": "buy",
        "setup": kind.rstrip("s"),
        "entry": entry,
        "stop": stop,
        "target_1": target_1,
        "target_2": target_2,
        "size_pct": 3,
        "urgency": "this_week",
        "horizon": "swing",
        "thesis": f"{kind.replace('_',' ')} setup in {s.get('theme') or 'universe'}; {rsi_note}{', ' + vol_note if vol_note else ''}.",
        "invalidation": f"Break below {stop}",
    }


def _fallback(recommendations: list[dict], scan_result: dict, review: dict) -> dict:
    trade_ideas: list[dict] = []
    for r in recommendations:
        if r.get("action") in {"trim", "sell"} and r.get("conviction", 0) >= 3:
            trade_ideas.append(_exit_trade(r))
    for bucket in ("breakouts", "momentum_continuation", "pullbacks_to_support", "oversold_bounces"):
        for s in scan_result["buckets"].get(bucket, [])[:2]:
            if s.get("held"):
                continue
            idea = _entry_trade(s, bucket)
            if idea:
                trade_ideas.append(idea)
    return {
        "headline": "Rule-based brief (scanner + per-ticker). Synthesis LLM call failed - check workflow log.",
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
          candidates: dict, exposures: dict, scan_result: dict,
          headlines: list[dict] | None = None) -> dict:
    risk = risk_profile()
    headlines = headlines or []

    if not llm.available():
        out = _fallback(recommendations, scan_result, review)
        out["generated_for"] = date.today().isoformat()
        out["headlines"] = headlines[:15]
        return out

    payload = {
        "date": date.today().isoformat(),
        "risk": risk.get("investor", {}).get("risk_tolerance"),
        "themes": risk.get("preferences", {}).get("themes"),
        "macro": _macro_summary(macro),
        "news": [h["title"][:100] for h in headlines[:8]],
        "cash%": _round(exposures.get("cash_pct"), 1),
        "sec%": {k: _round(v, 0) for k, v in (exposures.get("sector_pct") or {}).items()},
        "book": [_condensed_position(r) for r in recommendations],
        "scan": _scanner_condensed(scan_result),
    }

    user_blob = (
        "Today's signals. Pick 5-10 best moves. Concrete trades with "
        "entry/stop/T1/size. Lead with highest-conviction call by ticker name.\n"
        f"{json.dumps(payload, separators=(',', ':'), default=str)}"
    )

    out = llm.chat_json(
        prompts.SYSTEM_DAILY_BRIEF, user_blob,
        model=llm.synthesis_model(),
        max_tokens=2200,
        temperature=0.4,
        tag="brief",
    )
    if not out or "trade_ideas" not in out or len(out.get("trade_ideas") or []) == 0:
        print("Synthesis LLM did not return usable JSON - using rule-based fallback.")
        out = _fallback(recommendations, scan_result, review)
    out["generated_for"] = date.today().isoformat()
    out["headlines"] = headlines[:15]
    return out
