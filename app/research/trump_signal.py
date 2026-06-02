"""Trump-mention signal -- bullish/bearish overlay on Presidential statements.

The President occasionally moves a single name with a public statement
("X is a great company, I love what they're doing" vs. "Y is a disaster,
I will tariff them"). When a *credibly reported* statement of this kind
appears in the news feed for a held or candidate ticker, it is a primary
signal -- but it's a departure from the rest of the gate's
disciplined-evidence design, so:

  - Recency-gated. Stale mentions are not signals. Default 30 days.
  - Confidence-gated. Below the configured floor (default 0.6), the
    mention is logged for review but does NOT pass.
  - Configurable. Every knob lives in ``risk_profile.yaml`` (gate
    section); the whole signal can be killed via ``trump_signal_enabled:
    false``.
  - Manual-override-able. ``trump_watchlist.yaml`` at the repo root lets
    a human seed or override a detection.
  - Shadow-tracked. Every firing is logged to the shadow ledger so the
    real-world edge becomes measurable instead of assumed.

Data source: news headlines as already classified by
``app.research.news_classifier``. That module's gpt-4o-mini pass labels
each item with ``trump_mention``/``trump_valence``/``trump_confidence``;
when the LLM is unavailable, a deterministic keyword fallback in the
same module supplies the labels.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml

from app.logging import get_logger

log = get_logger(__name__)

# Manual overrides file (committed, user-editable). Each entry seeds or
# overrides a Trump-mention detection. Schema documented in the README.
WATCHLIST_PATH = Path(__file__).resolve().parent.parent.parent / "trump_watchlist.yaml"

_DEFAULT_TTL_DAYS = 30
_DEFAULT_MIN_CONFIDENCE = 0.6
_MANUAL_CONFIDENCE = 1.0  # a human override is treated as ground truth
_VALID_VALENCES = ("endorse", "attack")


def _to_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    s = str(value)
    # Be lenient: accept both "2026-06-01" and "2026-06-01T..." plus
    # common RSS formats.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except (ValueError, TypeError):
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _today() -> date:
    return datetime.now(timezone.utc).date()


def load_watchlist(path: Path | None = None) -> list[dict]:
    """Load the manual overrides file. Missing/malformed -> []."""
    p = path or WATCHLIST_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or []
    except Exception as e:
        log.warning("trump watchlist: could not parse %s: %s", p, e)
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("ticker")]


def _manual_entry_for(ticker: str, watchlist: list[dict], today: date,
                      ttl_days: int) -> dict | None:
    """Return the most recent manual entry for ``ticker`` within TTL, or None."""
    cutoff = today - timedelta(days=ttl_days)
    matches = []
    for e in watchlist:
        if (e.get("ticker") or "").upper() != ticker.upper():
            continue
        valence = str(e.get("valence", "")).lower().strip()
        if valence not in _VALID_VALENCES:
            continue
        as_of = _to_date(e.get("as_of") or e.get("date"))
        if as_of is None or as_of < cutoff:
            continue
        matches.append((as_of, e, valence))
    if not matches:
        return None
    matches.sort(key=lambda m: m[0], reverse=True)
    as_of, e, valence = matches[0]
    return {
        "valence": valence,
        "as_of": as_of.isoformat(),
        "source": e.get("source") or "manual override (trump_watchlist.yaml)",
        "summary": e.get("note") or e.get("summary") or
                   f"manual {valence} override for {ticker}",
        "confidence": _MANUAL_CONFIDENCE,
    }


def _best_news_mention(ticker: str, classifications: Iterable[dict],
                        today: date, ttl_days: int,
                        min_confidence: float) -> tuple[dict | None, list[dict]]:
    """Pick the strongest qualifying news-derived mention.

    A "qualifying" mention is recent (within TTL) AND has confidence
    >= ``min_confidence``. Returns ``(best_or_None, low_confidence_log)``.
    The low-confidence log keeps any in-TTL mentions that fell below the
    confidence floor, for review/telemetry.
    """
    cutoff = today - timedelta(days=ttl_days)
    qualifying: list[tuple[date, dict]] = []
    low_conf: list[dict] = []
    for c in classifications or []:
        if not c.get("trump_mention"):
            continue
        valence = (c.get("trump_valence") or "").lower()
        if valence not in _VALID_VALENCES:
            continue
        pub = _to_date(c.get("published"))
        if pub is None or pub < cutoff:
            continue
        try:
            confidence = float(c.get("trump_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        record = {
            "valence": valence,
            "as_of": pub.isoformat(),
            "source": c.get("headline") or "",
            "summary": c.get("one_line_summary") or c.get("headline") or "",
            "confidence": confidence,
        }
        if confidence < min_confidence:
            low_conf.append(record)
            continue
        qualifying.append((pub, record))
    if not qualifying:
        return None, low_conf
    # Prefer the highest-confidence mention; tie-break by most recent.
    qualifying.sort(key=lambda x: (x[1]["confidence"], x[0]), reverse=True)
    return qualifying[0][1], low_conf


def evaluate(
    ticker: str,
    classifications: Iterable[dict] | None,
    *,
    manual_overrides_path: Path | None = None,
    today: date | None = None,
    ttl_days: int = _DEFAULT_TTL_DAYS,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
) -> dict:
    """Return the Trump-mention finding for ``ticker``.

    Output schema::

        {
          "mention": bool,        # True iff a qualifying mention exists
          "valence": "endorse" | "attack" | "none",
          "confidence": float,    # 0..1; 0 when mention is False
          "as_of": "<ISO date>" | None,
          "source": "<headline>",
          "summary": "<one-line>",
          "manual": bool,         # True iff sourced from the watchlist
          "low_confidence_seen": [list of in-TTL mentions below the
                                  confidence floor; empty when there are none]
        }

    Manual overrides take precedence over news-derived mentions: a
    user-supplied entry within TTL is treated as confidence 1.0.
    """
    today = today or _today()
    watchlist = load_watchlist(manual_overrides_path)

    manual = _manual_entry_for(ticker, watchlist, today, ttl_days)
    if manual is not None:
        return {
            "mention": True,
            "valence": manual["valence"],
            "confidence": manual["confidence"],
            "as_of": manual["as_of"],
            "source": manual["source"],
            "summary": manual["summary"],
            "manual": True,
            "low_confidence_seen": [],
        }

    best, low_conf = _best_news_mention(
        ticker, classifications or [], today, ttl_days, min_confidence)
    if best is None:
        return {
            "mention": False,
            "valence": "none",
            "confidence": 0.0,
            "as_of": None,
            "source": "",
            "summary": "",
            "manual": False,
            "low_confidence_seen": low_conf,
        }
    return {
        "mention": True,
        "valence": best["valence"],
        "confidence": best["confidence"],
        "as_of": best["as_of"],
        "source": best["source"],
        "summary": best["summary"],
        "manual": False,
        "low_confidence_seen": low_conf,
    }
