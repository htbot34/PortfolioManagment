"""Portfolio-level review: concentration, sector exposure, cash deployment."""
import json
from collections import defaultdict

from app.config import settings, risk_profile
from app.data import prices
from app.portfolio.store import Account
from app.research import prompts


def compute_exposures(account: Account) -> dict:
    qs = prices.quotes([p.ticker for p in account.positions])
    rows = []
    sector_value: dict[str, float] = defaultdict(float)
    total_mv = 0.0
    for p in account.positions:
        q = qs[p.ticker]
        priced = q.price is not None
        mv = (q.price * p.shares) if priced else 0.0
        if priced:
            total_mv += mv
            sector = q.sector or "Unknown"
            sector_value[sector] += mv
        rows.append({
            "ticker": p.ticker,
            "shares": p.shares,
            "cost_basis": p.cost_basis,
            "price": q.price,
            "market_value": mv if priced else None,
            "unrealized_pl": (mv - p.book_value) if priced else None,
            "unrealized_pl_pct": ((mv - p.book_value) / p.book_value * 100) if priced and p.book_value else None,
            "sector": q.sector or "Unknown",
        })
    portfolio_value = total_mv + account.cash
    for row in rows:
        row["weight_pct"] = (row["market_value"] / portfolio_value * 100) if portfolio_value else 0
    sector_pct = {s: v / portfolio_value * 100 for s, v in sector_value.items()} if portfolio_value else {}
    return {
        "positions": rows,
        "total_market_value": total_mv,
        "cash": account.cash,
        "portfolio_value": portfolio_value,
        "sector_pct": sector_pct,
        "cash_pct": (account.cash / portfolio_value * 100) if portfolio_value else 0,
    }


def review(account: Account) -> dict:
    exposures = compute_exposures(account)
    if not settings.anthropic_api_key:
        return {"exposures": exposures, "error": "ANTHROPIC_API_KEY not set"}
    from anthropic import Anthropic
    client = Anthropic(api_key=settings.anthropic_api_key)
    risk = risk_profile()
    msg = client.messages.create(
        model=settings.claude_research_model,
        max_tokens=1200,
        system=prompts.SYSTEM_PORTFOLIO_REVIEW,
        messages=[{
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    f"RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
                    f"EXPOSURES:\n{json.dumps(exposures, indent=2, default=float)}\n\n"
                    "Output the JSON review now."
                ),
            }],
        }],
    )
    raw = msg.content[0].text if msg.content else "{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"raw": raw}
    return {"exposures": exposures, "review": body}
