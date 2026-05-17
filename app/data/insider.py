"""SEC Form 4 insider transaction tracking via EDGAR.

For each ticker we count Form 4 filings in the last 30 days and surface
the most recent ones with a link. Free, no key.

Heavy parsing of the underlying XBRL transactions is intentionally skipped
to keep the build fast - the count plus links is enough to flag clusters
of activity for the analyst to look at.
"""
from datetime import date, timedelta

import httpx

from app.config import settings
from app.data.filings import _ticker_to_cik

_TIMEOUT = 20.0
_HEADERS = {"User-Agent": "PortfolioAdvisor research", "Accept-Encoding": "gzip, deflate"}


def _headers() -> dict:
    return {**_HEADERS, "User-Agent": settings.sec_user_agent}


def recent_form4(ticker: str, days: int = 30) -> dict:
    """Return summary of Form 4 filings in the lookback window."""
    try:
        mapping = _ticker_to_cik()
    except Exception:
        return {"count": 0, "filings": [], "error": "cik lookup failed"}
    cik = mapping.get(ticker.upper())
    if not cik:
        return {"count": 0, "filings": [], "error": "ticker not in CIK index"}
    try:
        r = httpx.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=_headers(), timeout=_TIMEOUT,
        )
        r.raise_for_status()
        sub = r.json()
    except Exception as e:
        return {"count": 0, "filings": [], "error": str(e)}
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filed_dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    cutoff = date.today() - timedelta(days=days)
    filings: list[dict] = []
    for i, form in enumerate(forms):
        if form not in ("4", "4/A"):
            continue
        try:
            d = date.fromisoformat(filed_dates[i])
        except Exception:
            continue
        if d < cutoff:
            continue
        accession = accessions[i]
        filings.append({
            "form": form,
            "filed": filed_dates[i],
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=10",
            "accession": accession,
        })
        if len(filings) >= 20:
            break
    return {
        "count": len(filings),
        "filings": filings,
        "lookback_days": days,
    }
