"""SEC EDGAR fetcher for the latest 10-K, 10-Q and recent 8-Ks.

EDGAR requires a custom User-Agent identifying the requester (see SEC_USER_AGENT
in .env). Filing text is cached to disk under .cache/filings/ by accession number.
"""
import json
import re
from pathlib import Path

import httpx

from app.config import settings

_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary}"

_CACHE = settings.cache_dir / "filings"
_CACHE.mkdir(exist_ok=True)


def _headers() -> dict:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _ticker_to_cik() -> dict[str, str]:
    cache = _CACHE / "ticker_cik.json"
    if cache.exists():
        return json.loads(cache.read_text())
    r = httpx.get(_TICKER_CIK_URL, headers=_headers(), timeout=20)
    r.raise_for_status()
    rows = r.json()
    mapping = {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in rows.values()}
    cache.write_text(json.dumps(mapping))
    return mapping


def recent_filings(ticker: str, forms: tuple[str, ...] = ("10-K", "10-Q", "8-K"),
                   max_per_form: int = 2) -> list[dict]:
    """Return a list of {form, filed, accession, primary_doc, url} for the given ticker."""
    try:
        mapping = _ticker_to_cik()
    except Exception:
        return []
    cik = mapping.get(ticker.upper())
    if not cik:
        return []
    try:
        r = httpx.get(_SUBMISSIONS_URL.format(cik=cik), headers=_headers(), timeout=20)
        r.raise_for_status()
        sub = r.json()
    except Exception:
        return []
    recent = sub.get("filings", {}).get("recent", {})
    out: list[dict] = []
    counts: dict[str, int] = {f: 0 for f in forms}
    for i, form in enumerate(recent.get("form", [])):
        if form not in forms or counts[form] >= max_per_form:
            continue
        accession = recent["accessionNumber"][i]
        primary = recent["primaryDocument"][i]
        acc_nodash = accession.replace("-", "")
        cik_int = str(int(cik))
        out.append({
            "form": form,
            "filed": recent["filingDate"][i],
            "accession": accession,
            "primary_doc": primary,
            "url": _FILING_URL.format(cik_int=cik_int, acc_nodash=acc_nodash, primary=primary),
        })
        counts[form] += 1
    return out


def fetch_filing_text(filing: dict, max_chars: int = 200_000) -> str:
    """Download the primary filing document and return stripped text (capped)."""
    cache_file = _CACHE / f"{filing['accession']}.txt"
    if cache_file.exists():
        return cache_file.read_text()[:max_chars]
    try:
        r = httpx.get(filing["url"], headers=_headers(), timeout=60)
        r.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        return f"[fetch failed: {e}]"
    cache_file.write_text(text)
    return text[:max_chars]
