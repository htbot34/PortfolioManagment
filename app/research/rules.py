"""Rule-based recommender. Always available, no API keys needed.

The output schema matches the LLM path so the rest of the pipeline doesn't care
which source produced the recommendation.
"""
from dataclasses import dataclass


@dataclass
class Signal:
    action: str        # hold | trim | sell | add | new_buy
    horizon: str       # swing | long_term
    conviction: int    # 1..5
    reasons: list[str]
    catalysts: list[str]
    risks: list[str]
    action_detail: str


def recommend(ticker: str, quote: dict, tech: dict, position_ctx: dict,
              risk: dict, news_count: int) -> dict:
    reasons: list[str] = []
    catalysts: list[str] = []
    risks: list[str] = []

    price = quote.get("price")
    pe = quote.get("pe_ratio")
    sma50 = tech.get("sma50")
    sma200 = tech.get("sma200")
    rsi = tech.get("rsi14")
    pct_off_high = tech.get("pct_off_52w_high")
    weight = position_ctx.get("weight_pct") or 0
    unrealized_pct = position_ctx.get("unrealized_pl_pct") or 0
    max_single = risk.get("constraints", {}).get("max_single_position_pct", 25)
    aggressive = risk.get("investor", {}).get("risk_tolerance") in ("aggressive", "very_aggressive")

    score = 0   # positive = bullish bias, negative = bearish
    action = "hold"
    conviction = 2
    horizon = "long_term"

    if sma50 and sma200 and price:
        if price > sma50 > sma200:
            score += 2
            reasons.append("Stacked uptrend (price > SMA50 > SMA200)")
        elif price < sma50 < sma200:
            score -= 2
            reasons.append("Downtrend (price < SMA50 < SMA200)")
            risks.append("Below long-term trend - capital preservation risk")

    if rsi is not None:
        if rsi >= 75:
            score -= 1
            reasons.append(f"RSI overbought at {rsi:.0f}")
            risks.append("Short-term mean-reversion risk after overbought reading")
        elif rsi <= 30:
            score += 1
            reasons.append(f"RSI oversold at {rsi:.0f}")
            catalysts.append("Oversold bounce potential")

    if pct_off_high is not None:
        if pct_off_high > -5:
            reasons.append("At/near 52-week high")
        elif pct_off_high < -30:
            reasons.append(f"{pct_off_high:.0f}% off 52w high")
            if score >= 0 and aggressive:
                catalysts.append("Deep pullback in an aggressive risk profile - opportunistic add zone")

    # Conviction-5 defense triggers
    if weight > 30:
        action = "trim"
        conviction = 5
        reasons.append(f"Weight {weight:.0f}% far above {max_single}% cap - urgent trim")
        return _build(action, horizon, conviction, reasons, catalysts, risks,
                      f"Trim immediately to bring weight under {max_single}%")

    if (sma200 and price and price < sma200
            and tech.get("stacked_downtrend")
            and pct_off_high is not None and pct_off_high < -30):
        action = "sell"
        conviction = 5
        reasons.append(
            f"Confirmed downtrend: below SMA200, stacked bearish, {pct_off_high:.0f}% off high"
        )
        risks.append("Long bag risk - structural break, no recovery signal")
        return _build(action, horizon, conviction, reasons, catalysts, risks,
                      "Exit fully on any bounce toward SMA50")

    if weight > max_single:
        reasons.append(f"Weight {weight:.0f}% exceeds {max_single}% cap")
        action = "trim"
        conviction = 4
        return _build(action, horizon, conviction, reasons, catalysts, risks,
                      f"Trim to bring weight under {max_single}% of portfolio")

    if unrealized_pct >= 50 and (rsi or 0) >= 70:
        action = "trim"
        conviction = 3
        reasons.append(f"Up {unrealized_pct:.0f}% and overbought - de-risk partial")
        return _build(action, horizon, conviction, reasons, catalysts, risks,
                      "Trim 20-30% to lock gains; keep core")

    if score >= 2:
        action = "add" if aggressive else "hold"
        conviction = 3 if score >= 3 else 2
        horizon = "long_term"
    elif score <= -2:
        action = "sell" if (price and sma200 and price < sma200 and (pct_off_high or 0) < -25) else "hold"
        conviction = 3
        horizon = "swing" if action == "sell" else "long_term"
    else:
        action = "hold"
        conviction = 2

    if news_count > 0:
        catalysts.append(f"{news_count} recent news items - watch for narrative shifts")
    if pe and pe > 80:
        risks.append(f"Elevated valuation (P/E {pe:.0f})")

    detail = {
        "hold": "Maintain current size; review on next earnings or 10%+ move",
        "add": "Scale in on dips toward SMA50; do not chase",
        "trim": "Reduce by 20-30% above current price",
        "sell": "Exit fully on bounce toward SMA200",
        "new_buy": "Initiate small starter position on confirmed trend",
    }[action]

    return _build(action, horizon, conviction, reasons, catalysts, risks, detail)


def _build(action: str, horizon: str, conviction: int,
           reasons: list[str], catalysts: list[str], risks: list[str],
           detail: str) -> dict:
    return {
        "action": action,
        "horizon": horizon,
        "conviction": conviction,
        "thesis": " ".join(reasons) or "No strong signal; default hold under aggressive profile.",
        "key_catalysts": catalysts,
        "key_risks": risks,
        "suggested_action_detail": detail,
        "source": "rules",
    }
