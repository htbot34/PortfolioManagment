"""Market regime detection.

``detect_regime(macro_payload, breadth_payload)`` is a pure classifier. The
companion ``gather_regime_inputs()`` builds those payloads from free data
(SPY / VIX / HYG / IEF price history + the breadth basket).

Regimes: risk_on, risk_off, chop, breakdown.

Classification is a simple scoring system - each regime is a checklist of
conditions; the regime whose checklist is most satisfied wins; ``chop`` is
the neutral default when nothing scores cleanly. Each rule is documented
inline below.
"""
from __future__ import annotations

from typing import Callable

from app.data.breadth_basket import BREADTH_BASKET
from app.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Input gathering (free data only)
# ---------------------------------------------------------------------------

def _default_provider(ticker: str):
    from app.data import prices
    return prices.history(ticker)


def _pct_return(close, lookback: int) -> float | None:
    if close is None or len(close) <= lookback:
        return None
    past = float(close.iloc[-1 - lookback])
    if not past:
        return None
    return (float(close.iloc[-1]) - past) / past * 100


def _spy_inputs(provider: Callable) -> dict:
    df = provider("SPY")
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return {}
    close = df["Close"].astype(float)
    price = float(close.iloc[-1])
    sma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
    sma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    # range_bound_10d: every one of the last 10 closes within 5% of the
    # 50-day SMA computed on that day.
    range_bound = None
    if len(close) >= 60:
        sma50_series = close.rolling(50).mean()
        recent = ((close - sma50_series).abs() / sma50_series).tail(10)
        range_bound = bool((recent < 0.05).all())
    return {
        "price": price, "sma50": sma50, "sma200": sma200,
        "ret_5d": _pct_return(close, 5), "ret_20d": _pct_return(close, 20),
        "range_bound_10d": range_bound,
    }


def _vix_inputs(provider: Callable) -> dict:
    df = provider("^VIX")
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return {}
    close = df["Close"].astype(float)
    return {
        "level": float(close.iloc[-1]),
        "change_5d_pct": _pct_return(close, 5),
        "avg_20d": float(close.tail(20).mean()) if len(close) >= 20 else None,
    }


def _hyg_ief_inputs(provider: Callable) -> dict:
    """Credit risk appetite proxy: HYG (high yield) / IEF (7-10y Treasury).

    A rising ratio = credit risk-on, falling = credit stress.
    """
    hyg = provider("HYG")
    ief = provider("IEF")
    for df in (hyg, ief):
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            return {"trend": "unknown", "ratio_change_20d_pct": None}
    import pandas as pd
    ratio = (hyg["Close"].astype(float) / ief["Close"].astype(float)).dropna()
    chg = _pct_return(ratio, 20)
    if chg is None:
        trend = "unknown"
    elif chg > 1.0:
        trend = "rising"
    elif chg < -1.0:
        trend = "falling"
    else:
        trend = "flat"
    return {"trend": trend, "ratio_change_20d_pct": chg}


def _breadth(provider: Callable) -> dict:
    """Percent of the breadth basket trading above its own 50-day SMA."""
    above = 0
    counted = 0
    for ticker in BREADTH_BASKET:
        df = provider(ticker)
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            continue
        close = df["Close"].astype(float)
        if len(close) < 50:
            continue
        counted += 1
        if float(close.iloc[-1]) > float(close.tail(50).mean()):
            above += 1
    pct = round(above / counted * 100, 1) if counted else None
    return {"pct_above_sma50": pct, "n_basket": counted}


def gather_regime_inputs(prices_provider: Callable | None = None) -> tuple[dict, dict]:
    """Build (macro_payload, breadth_payload) from free price data."""
    provider = prices_provider or _default_provider
    macro_payload = {
        "spy": _spy_inputs(provider),
        "vix": _vix_inputs(provider),
        "hyg_ief": _hyg_ief_inputs(provider),
    }
    breadth_payload = _breadth(provider)
    return macro_payload, breadth_payload


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _frac(conditions: list[bool]) -> float:
    """Fraction of conditions satisfied (None/falsey count as not satisfied)."""
    if not conditions:
        return 0.0
    return sum(1 for c in conditions if c) / len(conditions)


def detect_regime(macro_payload: dict, breadth_payload: dict) -> dict:
    """Classify the market regime. Pure function - no I/O."""
    spy = macro_payload.get("spy") or {}
    vix = macro_payload.get("vix") or {}
    hyg = macro_payload.get("hyg_ief") or {}
    breadth = (breadth_payload or {}).get("pct_above_sma50")

    price = spy.get("price")
    sma50 = spy.get("sma50")
    sma200 = spy.get("sma200")
    spy_above_50 = price is not None and sma50 is not None and price > sma50
    spy_above_200 = price is not None and sma200 is not None and price > sma200
    spy_stacked_up = (spy_above_50 and spy_above_200
                      and sma50 is not None and sma200 is not None and sma50 > sma200)
    spy_below_200 = price is not None and sma200 is not None and price < sma200
    vix_level = vix.get("level")
    vix_5d = vix.get("change_5d_pct")
    hyg_trend = hyg.get("trend")
    range_bound = spy.get("range_bound_10d")

    # ---- Regime checklists (each rule documented) -------------------------
    # risk_on: clean uptrend, calm vol, credit risk-on, broad participation.
    risk_on = _frac([
        bool(spy_stacked_up),
        vix_level is not None and vix_level < 18,
        hyg_trend == "rising",
        breadth is not None and breadth > 60,
    ])
    # risk_off: below the 50d but holding the 200d, vol elevated but not
    # panicked, breadth middling.
    risk_off = _frac([
        bool(spy_above_200 and not spy_above_50),
        vix_level is not None and 20 <= vix_level <= 28,
        breadth is not None and 40 <= breadth <= 60,
    ])
    # chop: SPY range-bound around the 50d, breadth dead-center.
    chop = _frac([
        range_bound is True,
        breadth is not None and 45 <= breadth <= 55,
    ])
    # breakdown: below the 200d, vol high or spiking, credit stress, breadth
    # collapsed.
    breakdown = _frac([
        bool(spy_below_200),
        (vix_level is not None and vix_level > 28)
        or (vix_5d is not None and vix_5d > 20),
        hyg_trend == "falling",
        breadth is not None and breadth < 35,
    ])

    scores = {"risk_on": risk_on, "risk_off": risk_off,
              "chop": chop, "breakdown": breakdown}
    regime = max(scores, key=lambda k: scores[k])
    best = scores[regime]
    # Nothing scored cleanly -> default to the neutral chop state.
    if best < 0.5:
        regime = "chop"
        best = scores["chop"]
    confidence = 3 if best >= 0.8 else (2 if best >= 0.6 else 1)

    factors = {
        "scores": {k: round(v, 2) for k, v in scores.items()},
        "spy_above_sma50": spy_above_50,
        "spy_above_sma200": spy_above_200,
        "spy_stacked_up": bool(spy_stacked_up),
        "vix_level": vix_level,
        "vix_change_5d_pct": vix_5d,
        "hyg_ief_trend": hyg_trend,
        "breadth_pct": breadth,
        "spy_range_bound_10d": range_bound,
    }
    return {
        "regime": regime,
        "confidence": confidence,
        "factors": factors,
        "summary": _summary(regime, confidence, factors),
    }


def _summary(regime: str, confidence: int, f: dict) -> str:
    vix = f.get("vix_level")
    breadth = f.get("breadth_pct")
    bits = []
    if vix is not None:
        bits.append(f"VIX {vix:.1f}")
    if breadth is not None:
        bits.append(f"breadth {breadth:.0f}%")
    label = {
        "risk_on": "Risk-on - trend up, participation broad",
        "risk_off": "Risk-off - below the 50-day, vol elevated",
        "chop": "Chop - range-bound, mixed signals",
        "breakdown": "Breakdown - below the 200-day, defense only",
    }.get(regime, regime)
    detail = f" ({', '.join(bits)})" if bits else ""
    return f"{label}{detail} [confidence {confidence}/3]"
