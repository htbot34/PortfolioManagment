"""Three-signal conviction gate orchestrator.

``evaluate()`` runs technical -> sector -> news in that order so the cheap
checks short-circuit before we burn a news fetch + classification on a
candidate that already failed elsewhere. A recommendation ``qualifies`` only
if all three pass.

Phase 1 additions:
  - the news signal now consumes semantic LLM classifications
    (``app.research.news_classifier``) instead of keyword matching.
  - an earnings-window hard block: opening or adding to a LONG within 3 US
    trading days of the company's next earnings report does not qualify.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from app.research import news_classifier, signals


def _extract_sector(payload: dict) -> str:
    """Find the sector string on either a scanner row or analyst payload.

    Scanner rows skip yfinance fundamentals (sector is None) but carry a
    ``theme`` we can translate to a SPDR sector via THEME_TO_SECTOR.
    """
    if not isinstance(payload, dict):
        return ""
    sector = (
        payload.get("sector")
        or (payload.get("quote") or {}).get("sector")
        or ""
    )
    if sector:
        return sector
    theme = payload.get("theme")
    if theme:
        return signals.THEME_TO_SECTOR.get(theme, "")
    return ""


def _trading_days_until(target: date) -> int:
    """Count Mon-Fri days from today (exclusive) to ``target`` (inclusive).

    Holidays are ignored - this is a deterministic, free approximation and a
    holiday only ever makes the count slightly generous, which is the safe
    direction for a "wait for earnings" block.
    """
    today = date.today()
    if target <= today:
        return 0
    days = 0
    d = today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def evaluate(
    ticker_payload: dict,
    direction: str,
    macro: dict,
    news_fetcher: Callable[[str], list[dict]] | None = None,
    action: str | None = None,
) -> dict:
    """Run all three signals against ``ticker_payload`` for ``direction``.

    ``news_fetcher`` is called lazily and only if technical + sector pass,
    saving HTTP roundtrips on candidates that already failed.

    ``action`` (buy / add / trim / sell) drives the earnings-window block:
    opening or adding to a LONG within 3 trading days of earnings does not
    qualify. Trims and sells are never blocked by the earnings window.

    Returns::

        {
          "qualifies": bool,
          "signals": {"technical": ..., "sector_momentum": ..., "news": ...},
          "summary": "technical=PASS sector=PASS news=fail",
          "earnings_block": "<reason>"   # only present when the block fired
        }

    The ``signals`` dict only contains keys for signals that were actually
    evaluated - if technical fails first, sector_momentum and news are absent.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    out_signals: dict[str, dict] = {}

    tech = signals.technical_signal(ticker_payload, direction)
    out_signals["technical"] = tech
    if not tech["pass"]:
        return _result(False, out_signals)

    sector = _extract_sector(ticker_payload)
    sec = signals.sector_momentum_signal(sector, macro, direction)
    out_signals["sector_momentum"] = sec
    if not sec["pass"]:
        return _result(False, out_signals)

    news_items = ticker_payload.get("news")
    if news_items is None and news_fetcher is not None:
        ticker = ticker_payload.get("ticker") or ""
        try:
            news_items = news_fetcher(ticker) if ticker else []
        except Exception:
            news_items = []
    ticker = ticker_payload.get("ticker", "")
    classifications = ticker_payload.get("news_classifications")
    if classifications is None:
        classifications = news_classifier.classify_news_items(ticker, news_items or [])
    nws = signals.news_signal(ticker, classifications, direction)
    out_signals["news"] = nws

    qualifies = tech["pass"] and sec["pass"] and nws["pass"]
    result = _result(qualifies, out_signals)

    # Earnings-window hard block: only for opening/adding to a long.
    if qualifies and direction == "long" and (action or "").lower() in ("buy", "add", "new_buy"):
        block = _earnings_block(ticker)
        if block:
            result["qualifies"] = False
            result["earnings_block"] = block
    return result


def _earnings_block(ticker: str) -> str | None:
    """Return a block reason if earnings are within 3 trading days, else None."""
    if not ticker:
        return None
    try:
        from app.data.calendar import next_earnings_date
        ed = next_earnings_date(ticker)
    except Exception:
        return None
    if ed is None:
        return None
    days = _trading_days_until(ed)
    if 0 <= days <= 3:
        return f"earnings within 3 trading days ({ed.isoformat()}); wait for the report"
    return None


def _result(qualifies: bool, sigs: dict[str, dict]) -> dict:
    parts = []
    for name in ("technical", "sector_momentum", "news"):
        if name in sigs:
            parts.append(f"{name}={'PASS' if sigs[name]['pass'] else 'fail'}")
    return {
        "qualifies": qualifies,
        "signals": sigs,
        "summary": " ".join(parts),
    }
