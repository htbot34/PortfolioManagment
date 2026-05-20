"""Company fundamentals via yfinance. Free, no key.

``get_fundamentals(ticker)`` pulls valuation + quality metrics from
``yfinance.Ticker(...).info`` and caches them for 7 days in
``fundamentals_cache.json`` at the repo root (these ratios don't move
minute-to-minute and the ``.info`` call is slow).

Every field degrades to ``None`` when yfinance doesn't have it - many small
caps lack PEG or forward P/E. Callers must tolerate None.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "fundamentals_cache.json"
_TTL_S = 7 * 24 * 3600

# yfinance .info key -> our field name
_FIELD_MAP = {
    "trailingPE": "trailing_pe",
    "forwardPE": "forward_pe",
    "trailingPegRatio": "peg_ratio",
    "enterpriseToEbitda": "ev_to_ebitda",
    "priceToSalesTrailing12Months": "price_to_sales",
    "profitMargins": "profit_margin",
    "returnOnEquity": "return_on_equity",
    "sector": "sector",
}
_FIELDS = tuple(_FIELD_MAP.values())


def _empty(ticker: str) -> dict:
    out = {f: None for f in _FIELDS}
    out["ticker"] = ticker.upper()
    return out


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, separators=(",", ":")))
    except Exception as e:
        log.warning("could not write fundamentals cache: %s", e)


def _coerce(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # yfinance occasionally returns NaN
    return f if f == f else None


def get_fundamentals(ticker: str) -> dict:
    """Return a fundamentals dict for ``ticker`` (7-day disk cache).

    Keys: trailing_pe, forward_pe, peg_ratio, ev_to_ebitda, price_to_sales,
    profit_margin, return_on_equity, sector, ticker. Missing -> None.
    """
    ticker = ticker.upper()
    cache = _load_cache()
    entry = cache.get(ticker)
    if entry:
        fetched = entry.get("_fetched_at", 0)
        if (datetime.now(timezone.utc).timestamp() - fetched) <= _TTL_S:
            return {k: v for k, v in entry.items() if k != "_fetched_at"}

    out = _empty(ticker)
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        log.warning("fundamentals fetch failed for %s: %s", ticker, e)
        info = {}
    for yf_key, field in _FIELD_MAP.items():
        if yf_key in info:
            out[field] = _coerce(info.get(yf_key))

    cache[ticker] = {**out, "_fetched_at": datetime.now(timezone.utc).timestamp()}
    _save_cache(cache)
    return out
