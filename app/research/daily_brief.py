"""Daily verdict. Most days the verdict is no_trade.

Pipeline:
  1. Build a compact data payload (macro + book + scanner + news).
  2. Ask the synthesis LLM whether today's market signals justify ANY
     high-conviction action.
  3. Validate the LLM's output - if conviction != 5 or required fields
     are missing, override to no_trade.
  4. If the LLM is unavailable, fall back to a rule-based gate that
     defaults to no_trade and only emits if signals are extreme.
"""
import json
from datetime import date

from app.config import risk_profile
from app.research import llm, prompts


def _round(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else v


def _condensed_position(rec: dict) -> dict:
    q = rec.get("quote") or {}
    p = rec.get("position") or {}
    t = rec.get("technicals") or {}
    e = rec.get("earnings") or {}
    c = rec.get("consensus") or {}
    out = {
        "tk": rec["ticker"],
        "px": _round(q.get("price")),
        "d%": _round(q.get("day_change_pct"), 2),
        "wt%": _round(p.get("weight_pct"), 1),
        "pl%": _round(p.get("unrealized_pl_pct"), 1),
        "rsi": _round(t.get("rsi14"), 0),
        "macd": _round(t.get("macd_hist"), 2),
        "off52": _round(t.get("pct_off_52w_high"), 1),
        "up": t.get("stacked_uptrend"),
        "dn": t.get("stacked_downtrend"),
        "er": e.get("date"),
        "erDay": e.get("days_away"),
        "rule": rec.get("action"),
        "rConv": rec.get("conviction"),
    }
    return {k: v for k, v in out.items() if v not in (None, "", False)}


def _macro_summary(macro: dict) -> dict:
    return {
        "idx": {k: {"px": _round(v.get("price"), 2), "d%": _round(v.get("day_change_pct"), 2)}
                for k, v in macro["indices"].items()},
        "lead": [f"{s['name']} {_round(s['day_change_pct'],1)}%" for s in macro["leaders"]],
        "lag": [f"{s['name']} {_round(s['day_change_pct'],1)}%" for s in macro["laggards"]],
    }


def _scanner_condensed(scan_result: dict) -> dict:
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
                    "pullbacks_to_support")
    buckets = {name: slim(scan_result["buckets"].get(name, []), 4) for name in keep_buckets
               if scan_result["buckets"].get(name)}
    return {
        "buckets": buckets,
        "down": slim(scan_result.get("top_movers_down", []), 4),
    }


def _macro_is_risk_off(macro: dict) -> bool:
    """Cheap heuristic to bias toward no_trade when tape is broken."""
    vix = (macro.get("indices") or {}).get("VIX", {}).get("price")
    if vix and vix > 22:
        return True
    spx = (macro.get("indices") or {}).get("SPX", {})
    return bool(spx.get("pct_off_52w_high") is not None and spx["pct_off_52w_high"] < -10)


def _validate_action(a: dict | None) -> dict | None:
    """Reject anything that doesn't meet the conviction-5 bar with full fields."""
    if not a or not isinstance(a, dict):
        return None
    required = ("ticker", "action", "entry", "stop", "target", "size_pct", "thesis", "invalidation")
    if any(not a.get(k) for k in required):
        return None
    if a.get("conviction") != 5:
        return None
    if a.get("action") not in ("buy", "sell", "add", "trim"):
        return None
    return a


def _no_trade(reason: str, macro_line: str = "", watching: list | None = None) -> dict:
    return {
        "verdict": "no_trade",
        "headline": f"No trade today. {reason}",
        "primary_action": None,
        "secondary_actions": [],
        "market_snapshot": macro_line,
        "watching": watching or [],
        "generated_for": date.today().isoformat(),
    }


def _rule_based_fallback(recommendations: list[dict], scan_result: dict, macro: dict) -> dict:
    """Strict rule-based default. Returns no_trade unless something is extreme."""
    macro_line = ""
    spx = (macro.get("indices") or {}).get("SPX", {})
    vix = (macro.get("indices") or {}).get("VIX", {})
    if spx.get("day_change_pct") is not None and vix.get("price") is not None:
        macro_line = f"SPX {spx['day_change_pct']:+.2f}%, VIX {vix['price']:.1f}."

    if _macro_is_risk_off(macro):
        return _no_trade("Macro is risk-off. Wait for trend stabilization.", macro_line)

    # Defense: any held position the per-ticker analyst rated conviction 5 sell/trim
    for r in recommendations:
        if r.get("conviction", 0) == 5 and r.get("action") in ("sell", "trim"):
            q = r.get("quote") or {}
            t = r.get("technicals") or {}
            price = q.get("price")
            atr = t.get("atr14") or (price * 0.05 if price else None)
            sma200 = t.get("sma200")
            return {
                "verdict": "defense",
                "headline": f"{r['ticker']}: {r['action']}. {r.get('thesis','')[:120]}",
                "primary_action": {
                    "ticker": r["ticker"],
                    "action": r["action"],
                    "entry": f"~${price:.2f}" if price else "market",
                    "stop": f"${(price + atr):.2f}" if (price and atr) else "n/a",
                    "target": f"${sma200:.2f}" if sma200 else "exit",
                    "size_pct": None,
                    "thesis": r.get("thesis", ""),
                    "invalidation": "",
                    "conviction": 5,
                },
                "secondary_actions": [],
                "market_snapshot": macro_line,
                "watching": [],
                "generated_for": date.today().isoformat(),
            }

    # Offense: only emit if the scanner has a textbook breakout with strong
    # volume confirmation AND non-extended RSI.
    for s in scan_result["buckets"].get("breakouts", []):
        if s.get("held"):
            continue
        rsi = s.get("rsi14") or 0
        vol = s.get("vol_ratio_20d") or 0
        if 55 <= rsi <= 68 and vol >= 1.8:
            price = s.get("price")
            atr = s.get("atr14") or (price * 0.04 if price else None)
            return {
                "verdict": "trade",
                "headline": f"Buy {s['ticker']}: 20-day breakout with {vol:.1f}x volume.",
                "primary_action": {
                    "ticker": s["ticker"],
                    "action": "buy",
                    "entry": f"~${price:.2f}" if price else "",
                    "stop": f"${(price - 1.5 * atr):.2f}" if (price and atr) else "",
                    "target": f"${(price * 1.20):.2f}" if price else "",
                    "size_pct": 5,
                    "thesis": (
                        f"{s['ticker']} broke the 20-day high on {vol:.1f}x average volume "
                        f"with RSI {rsi:.0f} - not yet extended. The setup is a textbook "
                        f"breakout entry. Theme: {s.get('theme') or 'growth'}."
                    ),
                    "invalidation": f"Daily close back below ${price - 1.5 * (atr or 0):.2f} on volume.",
                    "conviction": 5,
                },
                "secondary_actions": [],
                "market_snapshot": macro_line,
                "watching": [],
                "generated_for": date.today().isoformat(),
            }

    # Build a small watching list from the scanner so user has something to track
    watching = []
    for s in scan_result["buckets"].get("breakouts", [])[:2]:
        watching.append(f"{s['ticker']} - waiting for breakout retest")
    for s in scan_result["buckets"].get("oversold_bounces", [])[:2]:
        watching.append(f"{s['ticker']} - watching oversold reversal")

    return _no_trade("No setup meets the conviction bar.", macro_line, watching[:5])


def build(macro: dict, recommendations: list[dict], review: dict,
          candidates: dict, exposures: dict, scan_result: dict,
          headlines: list[dict] | None = None) -> dict:
    """Return today's verdict."""
    risk = risk_profile()
    headlines = headlines or []

    if not llm.available():
        return _rule_based_fallback(recommendations, scan_result, macro)

    payload = {
        "date": date.today().isoformat(),
        "risk": risk.get("investor", {}).get("risk_tolerance"),
        "themes": risk.get("preferences", {}).get("themes"),
        "macro": _macro_summary(macro),
        "macro_risk_off": _macro_is_risk_off(macro),
        "news": [h["title"][:90] for h in headlines[:8]],
        "cash%": _round(exposures.get("cash_pct"), 1),
        "book": [_condensed_position(r) for r in recommendations],
        "scan": _scanner_condensed(scan_result),
    }

    user_blob = (
        "Today's full data. Be the strict gatekeeper. Default no_trade. "
        "Only emit a primary_action at conviction 5 with full fields.\n"
        f"{json.dumps(payload, separators=(',', ':'), default=str)}"
    )

    raw = llm.chat_json(
        prompts.SYSTEM_DAILY_BRIEF, user_blob,
        model=llm.synthesis_model(),
        max_tokens=1800,
        temperature=0.2,
        tag="brief",
    )

    if not raw:
        # LLM failed - fall back to rule-based gate
        return _rule_based_fallback(recommendations, scan_result, macro)

    primary = _validate_action(raw.get("primary_action"))
    secondary = [a for a in (raw.get("secondary_actions") or []) if _validate_action(a)]

    verdict = raw.get("verdict")
    if verdict not in ("no_trade", "trade", "defense"):
        verdict = "no_trade"

    # If the model said trade but the action didn't validate, downgrade to no_trade.
    if verdict == "trade" and not primary:
        verdict = "no_trade"
        headline = "No trade today. No conviction-5 setup."
    else:
        headline = raw.get("headline") or ("No trade today." if verdict == "no_trade"
                                            else "Action ready.")

    return {
        "verdict": verdict,
        "headline": headline,
        "primary_action": primary,
        "secondary_actions": secondary[:2],
        "market_snapshot": raw.get("market_snapshot", ""),
        "watching": (raw.get("watching") or [])[:6],
        "generated_for": date.today().isoformat(),
    }
