"""New-position candidates.

Approach:
  1. Pull quotes + technicals for every ticker in the curated universe
     that the investor doesn't already hold.
  2. Score each by a simple aggressive-growth fitness function (uptrend stack,
     momentum but not overbought, reasonable drawdown from highs).
  3. Pass the top ~15 candidates to the LLM with the wealth-advisor persona
     and let it pick 3-5 with specific entry zones and thesis.
"""
import json

from app.config import risk_profile
from app.data import prices
from app.portfolio.store import Account
from app.research import llm, prompts, universe


def _score(quote: dict, tech: dict) -> float:
    """Higher = better fit for aggressive long/swing entry. Range roughly -5 to +5."""
    s = 0.0
    price = quote.get("price")
    sma50 = tech.get("sma50")
    sma200 = tech.get("sma200")
    rsi = tech.get("rsi14")
    pct_off_high = tech.get("pct_off_52w_high")
    if not price:
        return -99
    if sma50 and sma200:
        if price > sma50 > sma200:
            s += 2
        elif price < sma50 < sma200:
            s -= 2
    if rsi is not None:
        if 40 <= rsi <= 65:
            s += 1
        elif rsi >= 75:
            s -= 1
        elif rsi <= 30:
            s += 0.5
    if pct_off_high is not None:
        if -15 <= pct_off_high <= -3:
            s += 1
        elif pct_off_high < -40:
            s -= 1
    return s


def _shortlist(held: set[str], n: int = 15) -> list[dict]:
    out = []
    for ticker in universe.all_tickers(exclude=held):
        q = prices.quote(ticker).to_dict()
        t = prices.technicals(ticker)
        sc = _score(q, t)
        if sc <= -99:
            continue
        out.append({
            "ticker": ticker,
            "score": round(sc, 2),
            "price": q.get("price"),
            "sector": q.get("sector"),
            "rsi14": t.get("rsi14"),
            "sma50": t.get("sma50"),
            "sma200": t.get("sma200"),
            "pct_off_52w_high": t.get("pct_off_52w_high"),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:n]


def candidates(account: Account) -> dict:
    held = {p.ticker.upper() for p in account.positions}
    short = _shortlist(held, n=15)

    if not llm.available():
        return {
            "candidates": [
                {
                    "ticker": s["ticker"],
                    "thesis": f"Screen score {s['score']}; RSI {s['rsi14']:.0f}, " if s['rsi14'] else "Screen-only ranking. Set GITHUB_TOKEN for analyst commentary.",
                    "entry_zone": f"~${s['price']:.2f}" if s["price"] else "-",
                    "risk": "No LLM commentary available.",
                    "horizon": "swing",
                    "conviction": 2,
                }
                for s in short[:5]
            ],
            "screen_results": short,
        }

    risk = risk_profile()
    user = (
        f"INVESTOR RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
        f"ALREADY HELD (skip these): {sorted(held)}\n\n"
        f"SHORTLIST FROM SCREEN (sorted, best fit first):\n"
        f"{json.dumps(short, indent=2)}\n\n"
        "Pick 3-5 names from this shortlist and produce the JSON. You may "
        "decline a high-scoring name if you have a reason, and you may include "
        "any of these. Be specific with entry zones - reference SMA50/52w levels."
    )

    out = llm.chat_json(
        prompts.SYSTEM_CANDIDATES, user,
        model=llm.synthesis_model(),
        max_tokens=1500,
        tag="candidates",
    )
    if not out or "candidates" not in out:
        return {"candidates": [], "screen_results": short}
    out["screen_results"] = short
    return out
