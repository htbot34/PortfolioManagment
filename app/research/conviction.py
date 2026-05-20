"""Conviction gate orchestrator.

``evaluate()`` runs the technical signal first (a hard prerequisite - it can
never be substituted), then sector_momentum and news. A recommendation
``qualifies`` via one of two paths:

  PRIMARY PATH       all three primary signals (technical, sector, news) pass.
  INSIDER PROMOTION  exactly two of the three pass, technical is one of them,
                     and the insider cluster score for the direction is >= 2.
                     The failing signal must be sector or news, never technical.

Phase 1 added: semantic LLM news classification and the earnings-window
hard block. Phase 2 added: the insider-promotion path.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from app.research import insider_signal, news_classifier, signals


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


def _insider_signal(ticker: str, direction: str, payload: dict,
                    fetcher: Callable[[str], list[dict]] | None) -> dict:
    """Compute the insider cluster signal for ``direction``.

    Transactions come from ``payload['insider_transactions']`` if present,
    else from ``fetcher`` if given, else from ``insider.recent_form4_transactions``.
    """
    txns = payload.get("insider_transactions")
    if txns is None:
        if fetcher is not None:
            try:
                txns = fetcher(ticker)
            except Exception:
                txns = []
        else:
            try:
                from app.data import insider
                txns = insider.recent_form4_transactions(ticker)
            except Exception:
                txns = []
    txns = txns or []
    if direction == "long":
        sc = insider_signal.insider_cluster_score(ticker, txns)
    else:
        sc = insider_signal.insider_cluster_score_short(ticker, txns)
    sc["pass"] = sc.get("score", 0) >= 2
    return sc


def evaluate(
    ticker_payload: dict,
    direction: str,
    macro: dict,
    news_fetcher: Callable[[str], list[dict]] | None = None,
    action: str | None = None,
    insider_fetcher: Callable[[str], list[dict]] | None = None,
) -> dict:
    """Run the conviction gate against ``ticker_payload`` for ``direction``.

    Returns::

        {
          "qualifies": bool,
          "signals": {"technical": ..., "sector_momentum": ..., "news": ...,
                       "insider": ...},   # insider only present if evaluated
          "summary": "technical=PASS sector_momentum=PASS news=fail",
          "promoted_by_insider": bool,
          "earnings_block": "<reason>"   # only when the earnings block fired
        }

    Technical is evaluated first and short-circuits the whole gate on failure
    (it can never be substituted). When technical passes, both sector and news
    are always evaluated so the 2-of-3 promotion path can be assessed.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    out_signals: dict[str, dict] = {}

    tech = signals.technical_signal(ticker_payload, direction)
    out_signals["technical"] = tech
    if not tech["pass"]:
        # Technical is a hard prerequisite - no path qualifies without it.
        result = _result(False, out_signals)
        result["promoted_by_insider"] = False
        return result

    sector = _extract_sector(ticker_payload)
    sec = signals.sector_momentum_signal(sector, macro, direction)
    out_signals["sector_momentum"] = sec

    news_items = ticker_payload.get("news")
    if news_items is None and news_fetcher is not None:
        tk = ticker_payload.get("ticker") or ""
        try:
            news_items = news_fetcher(tk) if tk else []
        except Exception:
            news_items = []
    ticker = ticker_payload.get("ticker", "")
    classifications = ticker_payload.get("news_classifications")
    if classifications is None:
        classifications = news_classifier.classify_news_items(ticker, news_items or [])
    nws = signals.news_signal(ticker, classifications, direction)
    out_signals["news"] = nws

    primary_pass = sum(1 for s in (tech, sec, nws) if s["pass"])
    qualifies = primary_pass == 3
    promoted_by_insider = False

    # Insider-promotion path: exactly 2 of 3, technical must be one of them.
    if not qualifies and primary_pass == 2 and tech["pass"]:
        insider = _insider_signal(ticker, direction, ticker_payload, insider_fetcher)
        out_signals["insider"] = insider
        if insider.get("score", 0) >= 2:
            qualifies = True
            promoted_by_insider = True

    result = _result(qualifies, out_signals)
    result["promoted_by_insider"] = promoted_by_insider

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
    for name in ("technical", "sector_momentum", "news", "insider"):
        if name in sigs:
            parts.append(f"{name}={'PASS' if sigs[name]['pass'] else 'fail'}")
    out = {
        "qualifies": qualifies,
        "signals": sigs,
        "summary": " ".join(parts),
    }
    if "insider" in sigs and qualifies:
        out["annotation"] = "promoted on insider cluster"
    return out

