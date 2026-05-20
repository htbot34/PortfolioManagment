"""Insider cluster scoring -- the 4th conviction signal.

Open-market insider buying (Form 4 transaction code "P") by a CLUSTER of
distinct insiders is one of the few genuinely predictive free signals.
``insider_cluster_score`` turns a list of parsed Form 4 transactions into a
0-3 score for the bull case; ``insider_cluster_score_short`` does the mirror
for the bear case using sales ("S"), excluding scheduled 10b5-1 sales which
carry no signal.

Score tiers (bull):
  0  < 2 distinct buyers, OR < $100k total
  1  2 distinct buyers AND $100k-$500k
  2  2-3 distinct buyers AND $500k-$2M, OR 4+ distinct buyers (any amount),
     OR 2-3 distinct buyers AND > $2M
  3  4+ distinct buyers AND >= $1M, OR a single C-suite (CEO/CFO/COO) buy
     >= $1M with at least one other buyer
"""
from __future__ import annotations

from datetime import date, timedelta

# Substrings (lowercased role) that mark a C-suite officer.
_CSUITE_TERMS = (
    "chief executive", "ceo",
    "chief financial", "cfo",
    "chief operating", "coo",
    "president and ceo",
)

_MIN_DOLLARS = 100_000


def _within(transaction_date: str, cutoff: date) -> bool:
    if not transaction_date:
        return True  # don't drop undated transactions
    try:
        return date.fromisoformat(transaction_date) >= cutoff
    except (ValueError, TypeError):
        return True


def _is_csuite(role: str) -> bool:
    r = (role or "").lower()
    return any(term in r for term in _CSUITE_TERMS)


def _aggregate(txns: list[dict]) -> tuple[set[str], float]:
    buyers = {t.get("filer_name") for t in txns if t.get("filer_name")}
    total = sum((t.get("total_value") or 0.0) for t in txns)
    return buyers, total


def insider_cluster_score(ticker: str, insider_transactions: list[dict],
                          lookback_days: int = 30) -> dict:
    """Bull-case insider cluster score from open-market purchases ("P")."""
    cutoff = date.today() - timedelta(days=lookback_days)
    buys = [t for t in (insider_transactions or [])
            if t.get("transaction_code") == "P"
            and _within(t.get("transaction_date"), cutoff)]
    buyers, total = _aggregate(buys)
    n = len(buyers)

    csuite_1m = any(_is_csuite(t.get("role")) and (t.get("total_value") or 0) >= 1_000_000
                    for t in buys)

    score = _tier(n, total, csuite_1m)
    summary = _summary(n, total, score, "buyer", csuite_1m)
    return {
        "score": score,
        "distinct_buyers": n,
        "total_dollars": round(total, 2),
        "summary": summary,
    }


def insider_cluster_score_short(ticker: str, insider_transactions: list[dict],
                                lookback_days: int = 30) -> dict:
    """Bear-case insider cluster score from open-market sales ("S").

    Scheduled 10b5-1 sales are excluded -- they're pre-planned and carry no
    informational signal. Filer role is not weighted for sells (sales are
    often diversification, not a bearish call), so there's no C-suite tier-3.
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    sells = [t for t in (insider_transactions or [])
             if t.get("transaction_code") == "S"
             and not t.get("is_planned_10b5_1")
             and _within(t.get("transaction_date"), cutoff)]
    sellers, total = _aggregate(sells)
    n = len(sellers)

    score = _tier(n, total, csuite_1m=False)
    summary = _summary(n, total, score, "seller", False)
    return {
        "score": score,
        "distinct_sellers": n,
        "total_dollars": round(total, 2),
        "summary": summary,
    }


def _tier(n: int, total: float, csuite_1m: bool) -> int:
    """Map (distinct filers, total $, csuite-1M flag) onto the 0-3 tiers."""
    if n < 2 or total < _MIN_DOLLARS:
        return 0
    # tier 3
    if (n >= 4 and total >= 1_000_000) or (csuite_1m and n >= 2):
        return 3
    # tier 2
    if (2 <= n <= 3 and 500_000 <= total <= 2_000_000):
        return 2
    if n >= 4:
        return 2
    if 2 <= n <= 3 and total > 2_000_000:
        return 2
    # tier 1: n >= 2 and total in $100k-$500k (and not stronger above)
    return 1


def _summary(n: int, total: float, score: int, noun: str, csuite: bool) -> str:
    if n < 2:
        return f"no insider cluster ({n} {noun}{'s' if n != 1 else ''})"
    csuite_note = ", incl. C-suite buy" if csuite else ""
    return (f"insider cluster score {score}/3: {n} {noun}s, "
            f"${total:,.0f} last 30d{csuite_note}")
