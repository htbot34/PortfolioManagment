"""Recommendation sizing.

``compute_size`` returns a sizing dict for a candidate recommendation,
respecting the construction rules in ``risk_profile.yaml`` /
``constraints.py`` (25% max single position, 5% min cash buffer).

Action-specific defaults:

- ``new_buy``  : target 3% of portfolio value (5% if the conviction gate's
                 technical signal scored 3/3).
- ``add``      : raise the position's weight by 2-3 percentage points, capped
                 at 25%.
- ``trim``     : 30% of the existing position (50% if ``severity='severe'``).
- ``sell``     : exit the full position.

For trims and sells the result also carries ``unrealized_pnl_on_action`` so
the action card can show a passive P&L line. This is a display-only value -
do NOT use it to alter the recommendation.

The output dict shape::

    {
      "display": "<one-line human summary>",
      "shares": 12,                    # integer count to transact
      "dollars": 1850.50,              # absolute notional being transacted
      "weight_delta_pct": 3.0,         # change in position weight (+/-)
      "target_weight_pct": 5.0,        # post-trade weight if applicable
      "current_weight_pct": 0.0,
      "unrealized_pnl_on_action": -120.50,  # only for trim/sell
      "rejected": False,
      "rejection_reason": "",
      "downsized": False,              # True if constraint forced a smaller size
      "notes": [],
    }
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from app.config import risk_profile as load_risk_profile
from app.portfolio.store import Account, Position


def _f(x, default=0.0):
    return float(x) if x is not None else float(default)


def _portfolio_value(account: Account, current_prices: dict[str, float] | None = None) -> float:
    """Approximate portfolio value: cash plus mark-to-market of priced positions.

    ``current_prices`` is an optional ticker->price map; positions whose
    ticker is missing are valued at cost_basis (best-effort).
    """
    total = account.cash
    for p in account.positions:
        price = (current_prices or {}).get(p.ticker.upper(), p.cost_basis)
        total += price * p.shares
    return total


def _conviction_score(gate: dict | None) -> int:
    """Pull technical signal score out of a conviction gate result."""
    if not gate:
        return 0
    sig = (gate.get("signals") or {}).get("technical") or {}
    return int(sig.get("score") or 0)


def _constraints() -> dict:
    risk = load_risk_profile() or {}
    return (risk.get("constraints") or {})


def _max_single() -> float:
    return float(_constraints().get("max_single_position_pct", 25))


def _min_cash() -> float:
    return float(_constraints().get("min_cash_buffer_pct", 5))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_size(
    action: str,
    ticker: str,
    current_price: float | None,
    position: Position | None,
    account: Account,
    gate: dict | None = None,
    severity: str = "normal",
    current_prices: dict[str, float] | None = None,
) -> dict:
    """See module docstring."""
    out = _empty()
    if current_price is None or current_price <= 0:
        return _reject(out, "no current price")
    portfolio_value = _portfolio_value(account, current_prices)
    if portfolio_value <= 0:
        return _reject(out, "portfolio value is zero")

    act = action.lower()
    if act in ("buy", "new_buy"):
        return _size_new_buy(ticker, current_price, account, portfolio_value, gate)
    if act == "add":
        return _size_add(ticker, current_price, position, account, portfolio_value, gate)
    if act == "trim":
        return _size_trim(ticker, current_price, position, portfolio_value, severity)
    if act == "sell":
        return _size_sell(ticker, current_price, position, portfolio_value)
    return _reject(out, f"unsupported action '{action}'")


# ---------------------------------------------------------------------------
# Action-specific sizing
# ---------------------------------------------------------------------------

def _size_new_buy(
    ticker: str, price: float, account: Account, pv: float, gate: dict | None
) -> dict:
    target_pct = 5.0 if _conviction_score(gate) >= 3 else 3.0
    target_dollars = pv * target_pct / 100
    notes: list[str] = []
    downsized = False

    max_dollars_by_cash = max(0.0, account.cash - (_min_cash() / 100) * pv)
    if target_dollars > max_dollars_by_cash:
        if max_dollars_by_cash <= 0:
            return _reject(_empty(), f"insufficient cash; need to keep {_min_cash():.0f}% buffer")
        notes.append(f"downsized to keep cash buffer >= {_min_cash():.0f}%")
        target_dollars = max_dollars_by_cash
        downsized = True

    # 25% single-position cap (new buy can't exceed it either).
    max_dollars_by_cap = pv * _max_single() / 100
    if target_dollars > max_dollars_by_cap:
        notes.append(f"downsized to stay under {_max_single():.0f}% single-position cap")
        target_dollars = max_dollars_by_cap
        downsized = True

    shares = int(target_dollars // price)
    if shares <= 0:
        return _reject(_empty(), f"price ${price:.2f} too high for available dollars ${target_dollars:.0f}")
    dollars = shares * price
    target_weight = dollars / pv * 100
    cash_pct_used = dollars / max(account.cash, 1e-9) * 100
    display = (
        f"Deploy {cash_pct_used:.0f}% of cash -> ${dollars:,.0f} -> "
        f"{shares} shares (would be {target_weight:.1f}% of portfolio)"
    )
    return _ok(display, shares, dollars, weight_delta_pct=target_weight,
                target_weight_pct=target_weight, current_weight_pct=0.0,
                notes=notes, downsized=downsized)


def _size_add(
    ticker: str, price: float, position: Position | None,
    account: Account, pv: float, gate: dict | None,
) -> dict:
    notes: list[str] = []
    downsized = False
    current_dollars = (position.shares * price) if position else 0.0
    current_weight = current_dollars / pv * 100 if position else 0.0

    # Default add: lift weight by 2pp (3pp if technical score 3/3).
    weight_lift = 3.0 if _conviction_score(gate) >= 3 else 2.0
    target_weight = current_weight + weight_lift
    if target_weight > _max_single():
        notes.append(f"capped at {_max_single():.0f}% single-position limit")
        target_weight = _max_single()
        downsized = True
    weight_lift_actual = target_weight - current_weight
    if weight_lift_actual <= 0.05:
        return _reject(_empty(), "already at or above single-position cap; no room to add")

    add_dollars = pv * weight_lift_actual / 100
    max_dollars_by_cash = max(0.0, account.cash - (_min_cash() / 100) * pv)
    if add_dollars > max_dollars_by_cash:
        if max_dollars_by_cash <= 0:
            return _reject(_empty(), f"insufficient cash; need to keep {_min_cash():.0f}% buffer")
        notes.append(f"downsized to keep cash buffer >= {_min_cash():.0f}%")
        add_dollars = max_dollars_by_cash
        downsized = True

    shares = int(add_dollars // price)
    if shares <= 0:
        return _reject(_empty(), "share count rounds to zero at current price")
    dollars = shares * price
    target_weight_actual = (current_dollars + dollars) / pv * 100
    display = (
        f"Raise from {current_weight:.1f}% -> {target_weight_actual:.1f}% -> "
        f"${dollars:,.0f} -> {shares} shares"
    )
    return _ok(display, shares, dollars,
                weight_delta_pct=target_weight_actual - current_weight,
                target_weight_pct=target_weight_actual,
                current_weight_pct=current_weight,
                notes=notes, downsized=downsized)


def _size_trim(
    ticker: str, price: float, position: Position | None,
    pv: float, severity: str,
) -> dict:
    if not position or position.shares <= 0:
        return _reject(_empty(), "no existing position to trim")
    trim_pct = 50 if severity.lower() in ("severe", "high", "critical") else 30
    shares = max(1, int(math.ceil(position.shares * trim_pct / 100)))
    shares = min(shares, int(position.shares))
    dollars = shares * price
    current_weight = (position.shares * price) / pv * 100
    post_weight = ((position.shares - shares) * price) / pv * 100
    pnl = (price - position.cost_basis) * shares
    display = (
        f"Trim {trim_pct}% -> {shares} shares -> ${dollars:,.0f} freed "
        f"(weight {current_weight:.1f}% -> {post_weight:.1f}%)"
    )
    return _ok(display, shares, dollars,
                weight_delta_pct=post_weight - current_weight,
                target_weight_pct=post_weight,
                current_weight_pct=current_weight,
                unrealized_pnl_on_action=round(pnl, 2))


def _size_sell(
    ticker: str, price: float, position: Position | None, pv: float,
) -> dict:
    if not position or position.shares <= 0:
        return _reject(_empty(), "no existing position to sell")
    shares = int(position.shares)
    dollars = shares * price
    current_weight = (position.shares * price) / pv * 100
    pnl = (price - position.cost_basis) * shares
    display = f"Exit full position -> {shares} shares -> ${dollars:,.0f}"
    return _ok(display, shares, dollars,
                weight_delta_pct=-current_weight,
                target_weight_pct=0.0,
                current_weight_pct=current_weight,
                unrealized_pnl_on_action=round(pnl, 2))


# ---------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {
        "display": "",
        "shares": 0,
        "dollars": 0.0,
        "weight_delta_pct": None,
        "target_weight_pct": None,
        "current_weight_pct": None,
        "unrealized_pnl_on_action": None,
        "rejected": False,
        "rejection_reason": "",
        "downsized": False,
        "notes": [],
    }


def _ok(display: str, shares: int, dollars: float, **kwargs) -> dict:
    out = _empty()
    out.update({"display": display, "shares": shares, "dollars": round(dollars, 2)})
    out.update(kwargs)
    return out


def _reject(base: dict, reason: str) -> dict:
    out = dict(base)
    out["rejected"] = True
    out["rejection_reason"] = reason
    out["display"] = f"Rejected: {reason}"
    return out
