"""Portfolio-level review: concentration, sector exposure, cash deployment."""
import json
from collections import defaultdict

from app.config import risk_profile
from app.data import prices
from app.portfolio.store import Account
from app.research import llm, prompts


def compute_exposures(account: Account) -> dict:
    """Compute per-position market values, weights, and sector exposure.

    Positions whose quote fetch failed get a ``price_unavailable: True`` flag
    and are excluded from weight/sector aggregates. Weights are computed
    against the *priced* portfolio (priced positions + cash) so they sum to
    100 percent of what we can actually see. ``unpriced_count`` surfaces in
    the result so the UI can warn when concentration math is incomplete.
    """
    qs = prices.quotes([p.ticker for p in account.positions])
    rows = []
    sector_value: dict[str, float] = defaultdict(float)
    total_mv = 0.0
    unpriced_count = 0
    for p in account.positions:
        q = qs[p.ticker]
        priced = q.price is not None
        mv = (q.price * p.shares) if priced else None
        if priced:
            total_mv += mv
            sector_value[q.sector or "Unknown"] += mv
        else:
            unpriced_count += 1
        rows.append({
            "ticker": p.ticker,
            "shares": p.shares,
            "cost_basis": p.cost_basis,
            "price": q.price,
            "day_change_pct": q.day_change_pct,
            "market_value": mv,
            "unrealized_pl": (mv - p.book_value) if priced else None,
            "unrealized_pl_pct": ((mv - p.book_value) / p.book_value * 100) if priced and p.book_value else None,
            "sector": q.sector or "Unknown",
            "price_unavailable": not priced,
        })
    portfolio_value = total_mv + account.cash
    for row in rows:
        if row["price_unavailable"]:
            row["weight_pct"] = None
        elif portfolio_value:
            row["weight_pct"] = row["market_value"] / portfolio_value * 100
        else:
            row["weight_pct"] = 0
    sector_pct = {s: v / portfolio_value * 100 for s, v in sector_value.items()} if portfolio_value else {}
    return {
        "positions": rows,
        "total_market_value": total_mv,
        "cash": account.cash,
        "portfolio_value": portfolio_value,
        "sector_pct": sector_pct,
        "cash_pct": (account.cash / portfolio_value * 100) if portfolio_value else 0,
        "unpriced_count": unpriced_count,
        "priced_count": len(account.positions) - unpriced_count,
    }


def review(exposures: dict) -> dict:
    """Rule-based portfolio observations, optionally refined by LLM."""
    risk = risk_profile()
    constraints = risk.get("constraints", {})
    obs: list[str] = []
    changes: list[str] = []

    max_single = constraints.get("max_single_position_pct", 25)
    min_cash = constraints.get("min_cash_buffer_pct", 5)
    max_sector = constraints.get("max_sector_pct", 45)

    for row in exposures["positions"]:
        w = row.get("weight_pct")
        if w is not None and w > max_single:
            obs.append(f"{row['ticker']} is {w:.0f}% of portfolio (cap {max_single}%)")
            changes.append(f"Trim {row['ticker']} to bring weight under {max_single}%")
    if exposures.get("unpriced_count"):
        obs.append(f"{exposures['unpriced_count']} position(s) had no live price - concentration math is incomplete.")

    if exposures["cash_pct"] < min_cash:
        obs.append(f"Cash is {exposures['cash_pct']:.1f}% of portfolio (target >= {min_cash}%)")
        changes.append("Build cash buffer for dry powder on pullbacks")

    for sector, pct in exposures["sector_pct"].items():
        if pct > max_sector:
            obs.append(f"{sector} sector at {pct:.0f}% (cap {max_sector}%)")
            changes.append(f"Diversify outside {sector}")

    out = {"observations": obs, "suggested_changes": changes, "open_questions": []}

    if llm.available():
        user_blob = (
            f"RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
            f"EXPOSURES:\n{json.dumps(exposures, indent=2, default=float)}\n\n"
            f"RULE-BASED OBSERVATIONS:\n{json.dumps(out, indent=2)}\n\n"
            "Add anything substantive the rules missed. Output the JSON now."
        )
        refined = llm.chat_json(prompts.SYSTEM_PORTFOLIO_REVIEW, user_blob, max_tokens=900, tag="portfolio_review")
        if refined and "observations" in refined:
            out = refined
    return out
