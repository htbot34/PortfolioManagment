"""SEC EDGAR fetcher for the latest 10-K, 10-Q and recent 8-Ks.

EDGAR requires a custom User-Agent identifying the requester (see SEC_USER_AGENT
in .env). Filing text and parsed tables are cached to disk under .cache/filings/
by accession number.
"""
import io
import json
import re
import time
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from app.config import settings, sec_user_agent_is_placeholder
from app.logging import get_logger

log = get_logger(__name__)

_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary}"

_CACHE = settings.cache_dir / "filings"
_CACHE.mkdir(exist_ok=True)

# CIK index lives at the repo root so GitHub Actions commits it back and it
# survives across containers. The old `.cache/filings/ticker_cik.json`
# location is gitignored and was re-fetched on every cold start, which is
# how a single SEC 403 turned every insider lookup into a silent score-0.
_CIK_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "ticker_cik_cache.json"

# Last CIK-fetch failure reason, for diagnostics. None when the most recent
# resolution attempt succeeded (or no attempt has happened yet).
LAST_CIK_ERROR: str | None = None

# Tags whose content is iXBRL plumbing or page chrome - dropped before extraction.
_DROP_TAGS = ("script", "style", "noscript", "iframe")
_DROP_NS_PREFIXES = ("ix:",)

# Bump when ``_extract_text`` semantics change so old caches don't poison
# new builds. Filings themselves are immutable, but our extraction is not.
_EXTRACTION_VERSION = "v2"


class CIKLookupError(RuntimeError):
    """Raised when the ticker->CIK mapping is unavailable.

    Distinct from "ticker isn't in the SEC index" -- this signals that the
    whole index is gone (live fetch failed AND no usable cache), so EVERY
    insider lookup will return empty for reasons that have nothing to do
    with the actual filings.
    """


def _headers() -> dict:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _describe_fetch_error(e: Exception) -> str:
    """Diagnostic string for a CIK-index fetch failure.

    Surfaces the HTTP status code + a short body snippet so a 403 (SEC
    rejected the User-Agent) is distinguishable from a 429 (throttled) and
    from a transport error, with an actionable hint for the common cases.
    """
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            code = resp.status_code
        except Exception:
            code = "?"
        try:
            body = " ".join((resp.text or "").split())[:160]
        except Exception:
            body = ""
        hint = ""
        if code == 403:
            hint = (" -- SEC likely rejected the User-Agent; set SEC_USER_AGENT "
                    "to a real monitored 'Name email@domain'")
            if sec_user_agent_is_placeholder(settings.sec_user_agent):
                hint += " (current UA looks like a placeholder)"
        elif code == 429:
            hint = " -- throttled by SEC; back off or raise CIK_CACHE_TTL_DAYS"
        return f"{type(e).__name__}: HTTP {code}{hint}" + (f" | body: {body}" if body else "")
    return f"{type(e).__name__}: {e}"


def _cik_cache_path() -> Path:
    """Resolve at call time so tests can monkeypatch ``_CIK_CACHE_PATH``."""
    return _CIK_CACHE_PATH


def _read_cik_cache(path: Path) -> tuple[dict[str, str] | None, float | None]:
    """Return (mapping, age_seconds). Either may be None on miss."""
    if not path.exists():
        return None, None
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None, None
    # Two on-disk formats are accepted:
    #   {"fetched_at": <unix-ts>, "mapping": {...}}   (new)
    #   {"AAA": "0000001234", ...}                    (legacy)
    if isinstance(raw, dict) and "mapping" in raw and isinstance(raw["mapping"], dict):
        fetched = raw.get("fetched_at")
        age = (time.time() - float(fetched)) if isinstance(fetched, (int, float)) else None
        return raw["mapping"], age
    if isinstance(raw, dict):
        return raw, None  # legacy format: treat as ageless
    return None, None


def _write_cik_cache(path: Path, mapping: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"fetched_at": time.time(), "mapping": mapping},
            separators=(",", ":"),
        ))
    except Exception as e:
        log.warning("could not persist CIK index to %s: %s", path, e)


def _ticker_to_cik() -> dict[str, str]:
    """Return the ticker -> CIK mapping.

    Persists to the repo-root ``ticker_cik_cache.json`` so the GitHub
    Actions workflow commits it back and the next run starts warm. The
    live SEC fetch only fires when the cache is older than
    ``settings.cik_cache_ttl_days`` (default 7) or missing entirely.

    Raises ``CIKLookupError`` only when there is no live response AND no
    usable cache -- in that case callers can render a loud "insider data
    unavailable: <reason>" diagnostic instead of silently scoring 0.
    """
    global LAST_CIK_ERROR
    path = _cik_cache_path()
    mapping, age_s = _read_cik_cache(path)
    ttl_s = settings.cik_cache_ttl_days * 24 * 3600
    if mapping and (age_s is None or age_s < ttl_s):
        LAST_CIK_ERROR = None
        return mapping
    # Cache is stale or absent. Try a live refresh; fall back to whatever
    # cache we have if the network fails (stale is better than missing).
    try:
        r = httpx.get(_TICKER_CIK_URL, headers=_headers(), timeout=20)
        r.raise_for_status()
        rows = r.json()
        fresh = {
            row["ticker"].upper(): str(row["cik_str"]).zfill(10)
            for row in rows.values()
        }
    except Exception as e:
        # Never cache a negative/403 result - that would suppress recovery.
        # Serve whatever cache we have (stale beats nothing); only raise when
        # there is no cache at all, so a real outage is loud, not a silent 0.
        LAST_CIK_ERROR = f"CIK index fetch failed: {_describe_fetch_error(e)}"
        log.warning("%s", LAST_CIK_ERROR)
        if mapping:
            log.warning("serving stale CIK cache (age=%.0fs) - resolution preserved",
                        age_s or 0)
            return mapping
        raise CIKLookupError(LAST_CIK_ERROR) from e
    _write_cik_cache(path, fresh)
    LAST_CIK_ERROR = None
    return fresh


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
