"""Three-signal conviction gate.

A recommendation surfaces only when all three signals pass:

  1. ``technical_signal``       — does the chart agree with the direction?
  2. ``news_signal``            — is there a recent supportive catalyst,
                                  with no major contradicting headline?
  3. ``sector_momentum_signal`` — is the relevant sector ETF trending in
                                  the same direction (5d + 20d)?

Each function is pure - given the same inputs it returns the same output.
The orchestration (short-circuiting, news fetching, etc.) lives in
``app/research/conviction.py``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

# yfinance / yahoo return sector names that don't always match the SPDR
# ETF coverage. Map several variants per ETF. Lowercased keys for lookup.
SECTOR_TO_ETF: dict[str, str] = {
    "technology": "XLK",
    "communication services": "XLC",
    "consumer cyclical": "XLY",
    "consumer discretionary": "XLY",
    "consumer defensive": "XLP",
    "consumer staples": "XLP",
    "financial services": "XLF",
    "financials": "XLF",
    "energy": "XLE",
    "healthcare": "XLV",
    "industrials": "XLI",
    "utilities": "XLU",
    "basic materials": "XLB",
    "materials": "XLB",
    "real estate": "XLRE",
}

# Scanner rows carry ``theme`` rather than ``sector`` (fundamentals are
# skipped for speed). Map theme strings to their dominant SPDR sector so
# the sector_momentum_signal can still apply.
THEME_TO_SECTOR: dict[str, str] = {
    "Mega cap tech": "Technology",
    "Semiconductors": "Technology",
    "AI infra / data": "Technology",
    "Cloud / SaaS": "Technology",
    "Cybersecurity": "Technology",
    "Speculative / high-beta": "Technology",
    "Fintech / payments": "Financial Services",
    "Bitcoin / digital assets infra": "Financial Services",
    "Healthcare / biotech": "Healthcare",
    "Consumer growth": "Consumer Cyclical",
    "Industrials growth": "Industrials",
    "Defense / aero": "Industrials",
    "SMR / nuclear / clean energy": "Utilities",
}


# Heuristic vocabulary for the news signal. Matched on lowercased headline
# tokens. Keep these tight - over-matching will let weak catalysts through.
_BULL_TERMS = {
    "beat", "beats", "raise", "raises", "raised", "upgrade", "upgraded",
    "outperform", "approval", "approved", "expansion", "expand", "acquire",
    "acquisition", "buyback", "buybacks", "partnership", "deal", "record",
    "surge", "rally", "strong", "exceed", "exceeds", "exceeded",
}
_BEAR_TERMS = {
    "miss", "missed", "downgrade", "downgraded", "cut", "cuts", "warning",
    "warns", "investigation", "lawsuit", "delay", "delayed", "weak",
    "weaker", "decline", "declines", "fraud", "fine", "fines", "violation",
    "resign", "resigns", "fired", "loss", "losses", "subpoena", "recall",
}

_WORD_RE = re.compile(r"[a-z][a-z\-']*")


# ---------------------------------------------------------------------------
# Technical signal
# ---------------------------------------------------------------------------

def technical_signal(payload: dict, direction: str = "long") -> dict:
    """Score the chart 0-3 in the direction of the trade. Pass if score >= 2.

    ``payload`` may be a scanner row (flat keys like ``rsi14``, ``macd_hist``)
    OR a per-ticker analyst payload with a ``technicals`` sub-dict. Both are
    accepted; flat scanner keys win when present.
    """
    t: dict = {}
    nested = payload.get("technicals") if isinstance(payload, dict) else None
    if isinstance(nested, dict):
        t.update(nested)
    if isinstance(payload, dict):
        for k in ("rsi14", "macd_hist", "macd_cross_up", "macd_cross_down",
                  "stacked_uptrend", "stacked_downtrend", "breakout_20d",
                  "golden_cross_recent", "death_cross_recent", "above_sma200",
                  "above_sma50", "pct_off_52w_high"):
            if payload.get(k) is not None:
                t[k] = payload[k]

    score = 0
    parts: list[str] = []

    rsi = t.get("rsi14")
    macd_h = t.get("macd_hist")
    cross_up = t.get("macd_cross_up")
    cross_dn = t.get("macd_cross_down")

    if direction == "long":
        if t.get("stacked_uptrend") or (t.get("breakout_20d") and t.get("above_sma200")):
            score += 1
            parts.append("trend up / breakout above SMA200")
        if rsi is not None and 40 <= rsi <= 65:
            score += 1
            parts.append(f"RSI {rsi:.0f} in momentum band")
        elif rsi is not None and rsi <= 30:
            score += 1
            parts.append(f"RSI {rsi:.0f} deep oversold")
        if (macd_h is not None and macd_h > 0) or cross_up or t.get("golden_cross_recent"):
            score += 1
            parts.append("MACD/MA confirmation")
    elif direction == "short":
        if t.get("stacked_downtrend") or (t.get("pct_off_52w_high") is not None and t.get("pct_off_52w_high") < -25 and not t.get("above_sma200")):
            score += 1
            parts.append("trend down / below SMA200 with drawdown")
        if rsi is not None and rsi >= 70:
            score += 1
            parts.append(f"RSI {rsi:.0f} overbought")
        elif rsi is not None and 50 <= rsi < 70 and t.get("stacked_downtrend"):
            score += 1
            parts.append(f"RSI {rsi:.0f} in failed-rally band")
        if (macd_h is not None and macd_h < 0) or cross_dn or t.get("death_cross_recent"):
            score += 1
            parts.append("MACD/MA bearish")
    else:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    return {
        "pass": score >= 2,
        "score": score,
        "reason": "; ".join(parts) if parts else "no qualifying technical signals",
    }


# ---------------------------------------------------------------------------
# News signal
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _is_recent(item: dict, max_days: int = 14) -> bool:
    """Best-effort recency check. Returns True if we can't tell (don't drop)."""
    raw = item.get("published") or item.get("datetime")
    if not raw:
        return True
    try:
        if isinstance(raw, (int, float)):
            ts = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        else:
            s = str(raw)
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                        "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d"):
                try:
                    ts = datetime.strptime(s, fmt)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return True
    except Exception:
        return True
    return (datetime.now(timezone.utc) - ts) <= timedelta(days=max_days)


# Semantic news scoring weights (Phase 1 rewrite).
_DURABILITY_WEIGHT = {"short": 0.3, "medium": 0.7, "long": 1.0}
_DIRECTION_SIGN = {"bullish": 1, "bearish": -1, "neutral": 0}


def _classification_weight(c: dict) -> float:
    return (c.get("magnitude") or 0) * _DURABILITY_WEIGHT.get(c.get("durability"), 0.7)


def news_signal(
    ticker: str,
    classifications: Iterable[dict] | None,
    direction: str,
) -> dict:
    """Score recent semantic news classifications for ``direction``.

    ``classifications`` is the output of
    ``app.research.news_classifier.classify_news_items`` - each carries
    ``direction``, ``magnitude`` (1-5), ``durability`` (short/medium/long),
    ``one_line_summary``, and ``published``.

    Net score = sum over the last 14 days of
    ``direction_sign * magnitude * durability_weight`` where the durability
    weights are short=0.3, medium=0.7, long=1.0.

    LONG passes when net >= 3 AND at least one item has magnitude >= 3 AND
    no bearish item with magnitude >= 4 appeared in the last 7 days.
    SHORT passes when net <= -3 AND at least one item has magnitude >= 3.
    ``evidence_refs`` lists the top 3 items by |magnitude * durability_weight|.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    recent_14 = [c for c in (classifications or []) if _is_recent(c, 14)]
    if not recent_14:
        return {"pass": False, "reason": "no recent classified news", "evidence_refs": []}

    net = 0.0
    for c in recent_14:
        sign = _DIRECTION_SIGN.get(c.get("direction"), 0)
        net += sign * (c.get("magnitude") or 0) * _DURABILITY_WEIGHT.get(c.get("durability"), 0.7)

    has_mag3 = any((c.get("magnitude") or 0) >= 3 for c in recent_14)
    top = sorted(recent_14, key=_classification_weight, reverse=True)[:3]
    refs = [c.get("one_line_summary") or c.get("headline") or "" for c in top]

    if direction == "long":
        recent_7 = [c for c in recent_14 if _is_recent(c, 7)]
        major_bear = any(c.get("direction") == "bearish" and (c.get("magnitude") or 0) >= 4
                          for c in recent_7)
        if net >= 3 and has_mag3 and not major_bear:
            return {"pass": True,
                    "reason": f"net news score {net:.1f} (bullish, {len(recent_14)} items)",
                    "evidence_refs": refs}
        why = []
        if net < 3:
            why.append(f"net score {net:.1f} < 3")
        if not has_mag3:
            why.append("no item magnitude >= 3")
        if major_bear:
            why.append("major bearish item (magnitude >= 4) in last 7 days")
        return {"pass": False, "reason": "; ".join(why), "evidence_refs": []}

    # short
    if net <= -3 and has_mag3:
        return {"pass": True,
                "reason": f"net news score {net:.1f} (bearish, {len(recent_14)} items)",
                "evidence_refs": refs}
    why = []
    if net > -3:
        why.append(f"net score {net:.1f} > -3")
    if not has_mag3:
        why.append("no item magnitude >= 3")
    return {"pass": False, "reason": "; ".join(why), "evidence_refs": []}


# ---------------------------------------------------------------------------
# Sector momentum signal
# ---------------------------------------------------------------------------

def _normalize_sector(sector: str) -> str:
    if not sector:
        return ""
    return sector.strip().lower()


def sector_momentum_signal(sector: str, macro: dict, direction: str) -> dict:
    """Pass if the sector ETF's 5d AND 20d returns align with direction.

    Unknown sectors return ``pass=False`` rather than silently passing - the
    conviction gate should be conservative when sector classification is
    missing.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    etf = SECTOR_TO_ETF.get(_normalize_sector(sector))
    if not etf:
        return {"pass": False, "reason": f"unknown sector '{sector}'"}
    sectors = (macro or {}).get("sectors") or {}
    target = next((d for d in sectors.values() if d.get("ticker") == etf), None)
    if not target:
        return {"pass": False, "reason": f"{etf} missing from macro snapshot"}
    r5, r20 = target.get("ret_5d"), target.get("ret_20d")
    if r5 is None or r20 is None:
        return {"pass": False, "reason": f"{etf} missing return data"}
    if direction == "long" and r5 > 0 and r20 > 0:
        return {"pass": True, "reason": f"{etf} +{r5:.1f}% 5d / +{r20:.1f}% 20d (long-aligned)"}
    if direction == "short" and r5 < 0 and r20 < 0:
        return {"pass": True, "reason": f"{etf} {r5:.1f}% 5d / {r20:.1f}% 20d (short-aligned)"}
    return {
        "pass": False,
        "reason": f"{etf} 5d {r5:+.1f}% / 20d {r20:+.1f}% - not aligned for {direction}",
    }
