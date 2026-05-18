"""Deterministic constraint checks for the portfolio.

Given the exposures dict from ``portfolio_review.compute_exposures`` and the
investor's risk profile, return a list of structured breaches:

    {
      "type":     "single_position" | "sector" | "cash" | "data",
      "severity": "warn" | "breach",
      "subject":  "META" | "Technology" | "cash" | "<ticker>",
      "current_pct":  float | None,
      "limit_pct":    float | None,
      "suggested_action": "Trim META to bring weight under 25%",
    }

The output is consumed by the dashboard banner, the daily-brief defense
gate, and (when wired) the LLM prompts. ``warn`` fires at 80% of the limit;
``breach`` fires when the limit is exceeded.
"""
from __future__ import annotations


def _warn_threshold(limit: float, ratio: float = 0.8) -> float:
    return limit * ratio


def check_constraints(exposures: dict, risk_profile_dict: dict) -> list[dict]:
    """Return all breaches sorted severity-first then by current_pct desc."""
    constraints = (risk_profile_dict or {}).get("constraints", {}) or {}
    max_single = float(constraints.get("max_single_position_pct", 25))
    max_sector = float(constraints.get("max_sector_pct", 45))
    min_cash = float(constraints.get("min_cash_buffer_pct", 5))

    breaches: list[dict] = []
    portfolio_value = float(exposures.get("portfolio_value") or 0)
    positions = exposures.get("positions") or []

    # Single-position concentration
    for row in positions:
        weight = row.get("weight_pct")
        if weight is None:
            continue
        if weight >= max_single:
            shares_to_trim = _shares_to_trim_to(weight, max_single, row)
            breaches.append({
                "type": "single_position",
                "severity": "breach",
                "subject": row["ticker"],
                "current_pct": round(weight, 2),
                "limit_pct": max_single,
                "suggested_action": (
                    f"Trim {row['ticker']} by ~{shares_to_trim} share(s) "
                    f"to bring weight under {max_single:.0f}%"
                    if shares_to_trim else
                    f"Trim {row['ticker']} to bring weight under {max_single:.0f}%"
                ),
            })
        elif weight >= _warn_threshold(max_single):
            breaches.append({
                "type": "single_position",
                "severity": "warn",
                "subject": row["ticker"],
                "current_pct": round(weight, 2),
                "limit_pct": max_single,
                "suggested_action": (
                    f"Monitor {row['ticker']}; approaching the {max_single:.0f}% cap"
                ),
            })

    # Sector concentration
    for sector, pct in (exposures.get("sector_pct") or {}).items():
        if pct >= max_sector:
            breaches.append({
                "type": "sector",
                "severity": "breach",
                "subject": sector,
                "current_pct": round(pct, 2),
                "limit_pct": max_sector,
                "suggested_action": f"Reduce {sector} exposure below {max_sector:.0f}%",
            })
        elif pct >= _warn_threshold(max_sector):
            breaches.append({
                "type": "sector",
                "severity": "warn",
                "subject": sector,
                "current_pct": round(pct, 2),
                "limit_pct": max_sector,
                "suggested_action": f"Monitor {sector} exposure; approaching {max_sector:.0f}% cap",
            })

    # Cash buffer (under-cash is the breach direction)
    cash_pct = float(exposures.get("cash_pct") or 0)
    if cash_pct < min_cash:
        breaches.append({
            "type": "cash",
            "severity": "breach",
            "subject": "cash",
            "current_pct": round(cash_pct, 2),
            "limit_pct": min_cash,
            "suggested_action": f"Build cash buffer to at least {min_cash:.0f}% for dry powder",
        })

    # Data gaps (visibility, not investment): flag unpriced positions
    unpriced = exposures.get("unpriced_count") or 0
    if unpriced:
        breaches.append({
            "type": "data",
            "severity": "warn",
            "subject": "price_feed",
            "current_pct": None,
            "limit_pct": None,
            "suggested_action": (
                f"{unpriced} position(s) had no live price; concentration math is incomplete this run"
            ),
        })

    breaches.sort(key=lambda b: (b["severity"] != "breach", -(b.get("current_pct") or 0)))
    return breaches


def _shares_to_trim_to(current_pct: float, limit_pct: float, row: dict) -> int | None:
    """Rough estimate of how many shares to sell to land just under the limit."""
    price = row.get("price")
    shares = row.get("shares")
    if not price or not shares or current_pct <= 0:
        return None
    target_ratio = limit_pct / current_pct
    new_shares = shares * target_ratio
    return max(1, int(round(shares - new_shares)))
