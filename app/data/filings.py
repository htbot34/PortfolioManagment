"""SEC EDGAR fetcher for the latest 10-K, 10-Q and recent 8-Ks.

EDGAR requires a custom User-Agent identifying the requester (see SEC_USER_AGENT
in .env). Filing text and parsed tables are cached to disk under .cache/filings/
by accession number.
"""
import io
import json
import re
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary}"

_CACHE = settings.cache_dir / "filings"
_CACHE.mkdir(exist_ok=True)

# Tags whose content is iXBRL plumbing or page chrome - dropped before extraction.
_DROP_TAGS = ("script", "style", "noscript", "iframe")
_DROP_NS_PREFIXES = ("ix:",)

# Bump when ``_extract_text`` semantics change so old caches don't poison
# new builds. Filings themselves are immutable, but our extraction is not.
_EXTRACTION_VERSION = "v2"


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
    except Exception as e:
        log.warning("ticker->CIK mapping failed: %s", e)
        return []
    cik = mapping.get(ticker.upper())
    if not cik:
        return []
    try:
        r = httpx.get(_SUBMISSIONS_URL.format(cik=cik), headers=_headers(), timeout=20)
        r.raise_for_status()
        sub = r.json()
    except Exception as e:
        log.warning("EDGAR submissions fetch failed for %s: %s", ticker, e)
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


def _drop_noise(soup: BeautifulSoup) -> None:
    """Strip elements that aren't body text: scripts, styles, iXBRL headers."""
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    # Inline-XBRL elements (ix:*) - find any tag whose name starts with ix:
    for tag in soup.find_all(lambda t: t.name and any(t.name.startswith(p) for p in _DROP_NS_PREFIXES)):
        tag.decompose()


def _extract_text(html: str, max_chars: int) -> str:
    """Pull readable prose + table rows out of an EDGAR HTML document.

    Order preserved. Block elements (p, li, td, th) terminated with a newline
    so table rows stay scannable instead of collapsing to one long line.
    """
    soup = BeautifulSoup(html, "lxml")
    _drop_noise(soup)
    parts: list[str] = []
    for el in soup.find_all(["p", "li", "td", "th", "h1", "h2", "h3", "h4"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        parts.append(text)
    joined = "\n".join(parts)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined[:max_chars]


def fetch_filing_text(filing: dict, max_chars: int = 200_000) -> str:
    """Download the primary filing document and return readable text (capped).

    Cached on disk under ``.cache/filings/{accession}_{version}.txt``.
    Filings are immutable but extraction logic isn't - the version suffix
    invalidates stale caches automatically when ``_EXTRACTION_VERSION``
    changes.
    """
    cache_file = _CACHE / f"{filing['accession']}_{_EXTRACTION_VERSION}.txt"
    if cache_file.exists():
        return cache_file.read_text()[:max_chars]
    try:
        r = httpx.get(filing["url"], headers=_headers(), timeout=60)
        r.raise_for_status()
        text = _extract_text(r.text, max_chars)
    except Exception as e:
        log.warning("filing text fetch failed for %s: %s", filing.get("accession"), e)
        return f"[fetch failed: {e}]"
    cache_file.write_text(text)
    return text


def fetch_filing_tables(filing: dict, max_tables: int = 20) -> list[pd.DataFrame]:
    """Return a list of pandas DataFrames extracted from the filing's tables.

    Uses ``pd.read_html`` on the (de-noised) HTML. Skips empty or single-cell
    tables. Cached as a pickled CSV bundle on disk by accession.
    """
    cache_file = _CACHE / f"{filing['accession']}_{_EXTRACTION_VERSION}.tables.json"
    if cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text())
            return [pd.read_json(io.StringIO(t)) for t in payload]
        except Exception as e:
            log.debug("table cache reload failed: %s", e)
    try:
        r = httpx.get(filing["url"], headers=_headers(), timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        _drop_noise(soup)
        tables = pd.read_html(io.StringIO(str(soup)))
    except Exception as e:
        log.warning("table extraction failed for %s: %s", filing.get("accession"), e)
        return []
    out: list[pd.DataFrame] = []
    for df in tables:
        if df is None or df.empty or df.shape == (1, 1):
            continue
        out.append(df)
        if len(out) >= max_tables:
            break
    try:
        cache_file.write_text(json.dumps([df.to_json() for df in out]))
    except Exception as e:
        log.debug("table cache write failed: %s", e)
    return out
