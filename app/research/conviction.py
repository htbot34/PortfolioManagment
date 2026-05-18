"""Three-signal conviction gate orchestrator.

``evaluate()`` runs technical -> sector -> news in that order so the cheap
checks short-circuit before we burn a news fetch on a candidate that already
failed elsewhere. A recommendation ``qualifies`` only if all three pass.
"""
from __future__ import annotations

from typing import Callable

from app.research import signals


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


def evaluate(
    ticker_payload: dict,
    direction: str,
    macro: dict,
    news_fetcher: Callable[[str], list[dict]] | None = None,
) -> dict:
    """Run all three signals against ``ticker_payload`` for ``direction``.

    ``news_fetcher`` is called lazily and only if technical + sector pass,
    saving HTTP roundtrips on candidates that already failed.

    Returns::

        {
          "qualifies": bool,
          "signals": {"technical": ..., "sector_momentum": ..., "news": ...},
          "summary": "technical=PASS sector=PASS news=fail",
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
    filings_summary = ticker_payload.get("filings_summary")
    nws = signals.news_signal(
        ticker_payload.get("ticker", ""), news_items or [], filings_summary, direction
    )
    out_signals["news"] = nws

    return _result(tech["pass"] and sec["pass"] and nws["pass"], out_signals)


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
