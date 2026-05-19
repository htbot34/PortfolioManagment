"""Derive a ``user_preferences_context`` from the last ~30 days of rec_history.

Two patterns drive future behavior:

1. **Repeated rejections.** When the user has rejected >= 2 recs for the same
   ticker in the lookback window, future recs on that ticker get a *soft veto*
   - the deterministic gate downgrades them to ``watching`` instead of
   promoting them to a primary_action, unless conditions have *materially*
   changed (no helper for "materially" yet; for now any same-ticker repeat
   triggers the demotion).

2. **Counter-proposal patterns.** When the user counter-proposes sizing changes
   >= 2 times on the same ticker, capture the reason strings and example
   counter payloads; downstream code can adjust default sizing or simply
   surface the note alongside future recs.

The output is intentionally small and JSON-serializable so it can be passed
straight into an LLM system prompt later without further shaping.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable


def _is_recent(entry: dict, lookback_days: int = 30) -> bool:
    d = entry.get("date") or ""
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    return d >= cutoff


def derive_user_preferences(
    history: Iterable[dict],
    lookback_days: int = 30,
    min_repeats: int = 2,
) -> dict:
    """Walk ``history`` and emit the preferences block."""
    by_ticker_rejected: dict[str, list[dict]] = defaultdict(list)
    by_ticker_countered: dict[str, list[dict]] = defaultdict(list)

    for entry in history or []:
        if not _is_recent(entry, lookback_days):
            continue
        status = entry.get("status")
        ticker = (entry.get("ticker") or "").upper()
        if not ticker:
            continue
        if status == "rejected":
            by_ticker_rejected[ticker].append(entry)
        elif status == "counter":
            by_ticker_countered[ticker].append(entry)

    repeat_rejected = []
    for ticker, items in by_ticker_rejected.items():
        if len(items) < min_repeats:
            continue
        reasons = [it.get("user_reason") for it in items if it.get("user_reason")]
        actions = sorted({it.get("action") for it in items if it.get("action")})
        repeat_rejected.append({
            "ticker": ticker,
            "count": len(items),
            "actions_rejected": actions,
            "reasons": reasons[:3],
            "last_rejected_at": max(it.get("resolved_at") or it.get("date") or ""
                                     for it in items),
        })

    counter_patterns = []
    for ticker, items in by_ticker_countered.items():
        if len(items) < min_repeats:
            continue
        reasons = [it.get("user_reason") for it in items if it.get("user_reason")]
        notes = []
        for it in items:
            cp = it.get("counter_proposal") or {}
            if cp:
                notes.append(cp)
        counter_patterns.append({
            "ticker": ticker,
            "count": len(items),
            "reasons": reasons[:3],
            "examples": notes[:3],
        })

    return {
        "lookback_days": lookback_days,
        "soft_vetoes": repeat_rejected,
        "counter_patterns": counter_patterns,
    }


def soft_veto_tickers(preferences: dict) -> set[str]:
    """Tickers the user has rejected enough times to suppress new recs."""
    return {row["ticker"] for row in (preferences or {}).get("soft_vetoes", [])}
