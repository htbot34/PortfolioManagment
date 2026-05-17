"""Upcoming earnings + recent analyst rating changes. Best-effort via yfinance.

Returns empty/None on any failure so the brief still renders.
"""
from datetime import date, datetime
from typing import Any


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
