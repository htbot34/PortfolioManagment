"""Upcoming earnings + recent analyst rating changes. Best-effort via yfinance.

Returns empty/None on any failure so the brief still renders.
"""
import json
from datetime import date, datetime, timezone
from typing import Any

from app.config import settings

_EARNINGS_CACHE = settings.cache_dir / "earnings.json"
_EARNINGS_TTL_S = 24 * 3600  # earnings dates are stable enough for a day


def _earnings_cache_get(ticker: str):
    """Return (hit: bool, value: date|None). value is None for 'no date known'."""
    if not _EARNINGS_CACHE.exists():
        return False, None
    try:
        cache = json.loads(_EARNINGS_CACHE.read_text())
        entry = cache.get(ticker.upper())
    except Exception:
        return False, None
    if not entry:
        return False, None
    fetched = entry.get("fetched_at", 0)
    if (datetime.now(timezone.utc).timestamp() - fetched) > _EARNINGS_TTL_S:
        return False, None
    d = entry.get("date")
    if not d:
        return True, None
    try:
        return True, date.fromisoformat(d)
    except ValueError:
        return True, None


def _earnings_cache_put(ticker: str, value: date | None) -> None:
    try:
        cache = {}
        if _EARNINGS_CACHE.exists():
            cache = json.loads(_EARNINGS_CACHE.read_text())
        cache[ticker.upper()] = {
            "date": value.isoformat() if value else None,
            "fetched_at": datetime.now(timezone.utc).timestamp(),
        }
        _EARNINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _EARNINGS_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass


def next_earnings_date(ticker: str) -> date | None:
    """Return the next earnings date as a ``date``, or None if unknown.

    Cached on disk for 24h (the cache stores a None result too, so we don't
    re-hit yfinance every build for tickers with no scheduled date).
    """
    hit, value = _earnings_cache_get(ticker)
    if hit:
        return value
    ed = earnings_date(ticker)
    result: date | None = None
    if ed and ed.get("date"):
        try:
            result = date.fromisoformat(ed["date"])
        except (ValueError, TypeError):
            result = None
    _earnings_cache_put(ticker, result)
    return result


def _to_date(v: Any) -> str | None:
    try:
        if v is None:
            return None
        if hasattr(v, "date"):
            return v.date().isoformat()
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, date):
            return v.isoformat()
        return str(v)[:10]
    except Exception:
        return None


def earnings_date(ticker: str) -> dict | None:
    """Return {date, days_away} for next earnings if available."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None
        # yfinance returns either a DataFrame or a dict depending on version
        if hasattr(cal, "to_dict"):
            cal = cal.to_dict()
        # Common keys: 'Earnings Date', 'earningsDate'
        for key in ("Earnings Date", "earningsDate", "Earnings"):
            if key in cal:
                val = cal[key]
                if isinstance(val, (list, tuple)) and val:
                    val = val[0]
                d = _to_date(val)
                if d:
                    try:
                        days = (date.fromisoformat(d) - date.today()).days
                    except Exception:
                        days = None
                    return {"date": d, "days_away": days}
    except Exception:
        return None
    return None


def analyst_recs(ticker: str, limit: int = 5) -> list[dict]:
    """Return recent analyst rating actions, newest first."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.upgrades_downgrades if hasattr(t, "upgrades_downgrades") else None
        if df is None or df.empty:
            return []
        df = df.tail(limit).iloc[::-1]
        out = []
        for idx, row in df.iterrows():
            out.append({
                "date": _to_date(idx),
                "firm": row.get("Firm") or row.get("firm"),
                "to_grade": row.get("ToGrade") or row.get("toGrade"),
                "from_grade": row.get("FromGrade") or row.get("fromGrade"),
                "action": row.get("Action") or row.get("action"),
            })
        return out
    except Exception:
        return []


def consensus(ticker: str) -> dict | None:
    """Mean analyst target and recommendation key."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return {
            "target_mean": info.get("targetMeanPrice"),
            "target_high": info.get("targetHighPrice"),
            "target_low": info.get("targetLowPrice"),
            "recommendation": info.get("recommendationKey"),
            "num_analysts": info.get("numberOfAnalystOpinions"),
        }
    except Exception:
        return None
