"""Swing-trade plan builder.

Given a price, ATR-based volatility and the setup type, produces a concrete
swing-trade plan: an entry zone, a stop-loss, a price target and an estimated
hold window. ATR (Average True Range) sizes the stop so the plan adapts to each
name's own volatility instead of using a fixed percentage.

Plans are advisory only -- no orders are placed.
"""
from __future__ import annotations

# stop sits this many ATRs below the low end of the entry zone
_STOP_ATR_MULT = 1.5
# target distance is this multiple of the risk (stop distance) -> reward:risk
_REWARD_RISK = 2.0
# assumed swing pace: a name nets ~this fraction of one ATR per trading day
_ATR_PER_DAY = 0.35


def _entry_zone(price: float, atr: float, setup_labels: list[str]) -> tuple[float, float]:
    """Setup-aware entry band. Momentum setups buy strength; pullback / oversold
    setups buy into weakness, so the band is skewed below the current price."""
    text = " ".join(setup_labels).lower()
    if "pullback" in text or "oversold" in text or "bounce" in text:
        low, high = price - 0.8 * atr, price + 0.2 * atr
    elif "breakout" in text or "52-week high" in text or "momentum" in text:
        low, high = price - 0.3 * atr, price + 0.5 * atr
    else:
        low, high = price - 0.4 * atr, price + 0.4 * atr
    return round(low, 2), round(high, 2)


def build(price: float | None, tech: dict | None,
          setup_labels: list[str] | None = None) -> dict | None:
    """Return a swing-trade plan, or ``None`` when there isn't enough data.

    Requires a positive price and a positive ATR (``atr14`` in ``tech``).
    """
    setup_labels = setup_labels or []
    atr = (tech or {}).get("atr14")
    if not price or price <= 0 or not atr or atr <= 0:
        return None

    entry_low, entry_high = _entry_zone(price, atr, setup_labels)
    entry_mid = (entry_low + entry_high) / 2.0
    stop = round(entry_low - _STOP_ATR_MULT * atr, 2)
    risk = entry_mid - stop
    if risk <= 0:
        return None
    target = round(entry_mid + _REWARD_RISK * risk, 2)

    days = (target - entry_mid) / (_ATR_PER_DAY * atr)
    days = max(5.0, min(45.0, days))
    weeks = days / 5.0
    lo_w = max(1, round(weeks * 0.7))
    hi_w = max(lo_w + 1, round(weeks * 1.3))

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_zone": f"${entry_low:.2f} - ${entry_high:.2f}",
        "stop": stop,
        "stop_pct": round((stop / entry_mid - 1.0) * 100.0, 1),
        "target": target,
        "target_pct": round((target / entry_mid - 1.0) * 100.0, 1),
        "reward_risk": round(_REWARD_RISK, 1),
        "hold_window": f"{lo_w}-{hi_w} weeks",
    }
