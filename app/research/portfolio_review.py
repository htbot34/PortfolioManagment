"""Portfolio-level review: concentration, sector exposure, cash deployment."""
import json
from collections import defaultdict

from app.config import risk_profile
from app.data import prices
from app.portfolio.store import Account
from app.research import constraints as constraints_mod
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
    """Rule-based portfolio observations + structured breaches, optionally refined by LLM.

    The structured breaches are produced by ``app.research.constraints`` and
    duplicated as human-readable observations / suggested changes so existing
    templates and the LLM prompt can both consume them.
    """
    risk = risk_profile()
    breaches = constraints_mod.check_constraints(exposures, risk)
    obs: list[str] = []
    changes: list[str] = []
    for b in breaches:
        if b["type"] == "data":
            obs.append(b["suggested_action"])
            continue
        pct = f"{b['current_pct']:.1f}%" if b.get("current_pct") is not None else ""
        cap = f"{b['limit_pct']:.0f}% cap" if b.get("limit_pct") is not None else ""
        sev = "BREACH" if b["severity"] == "breach" else "warn"
        obs.append(f"[{sev}] {b['subject']} {pct} ({cap})".strip())
        changes.append(b["suggested_action"])

    out = {
        "observations": obs,
        "suggested_changes": changes,
        "open_questions": [],
        "breaches": breaches,
    }

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
