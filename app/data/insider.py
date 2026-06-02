"""SEC Form 4 insider transaction tracking via EDGAR. Free, no key.

Two levels of detail:

- ``recent_form4(ticker, days)``  -- lightweight: counts Form 4 filings in
  the window. Kept for the per-ticker payload's quick "is there activity"
  flag.
- ``recent_form4_transactions(ticker, days)`` -- parses each Form 4's
  ownership XML into individual transactions: filer_name, role,
  transaction_date, transaction_code (P=purchase, S=sale, A=award, ...),
  acquired_disposed, shares, price, total_value, and is_planned_10b5_1.

Parsed transactions are cached on disk by accession number (Form 4 filings
are immutable once filed) so repeated builds don't re-fetch.
"""
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import settings
from app.data import filings
from app.data.filings import CIKLookupError, _ticker_to_cik
from app.logging import get_logger

log = get_logger(__name__)

_TIMEOUT = 20.0
_HEADERS = {"User-Agent": "PortfolioAdvisor research", "Accept-Encoding": "gzip, deflate"}
_FORM4_CACHE = settings.cache_dir / "form4"

# Per-ticker error reason from the most recent fetch. ``None`` means the
# last attempt succeeded (or no attempt has happened). Callers can read
# this to render a loud "insider data unavailable: <reason>" diagnostic
# instead of silently treating a fetch failure as score 0.
LAST_FETCH_ERRORS: dict[str, str] = {}

# 24h disk cache over the two EDGAR-bound entry points. EDGAR is frequently
# slow during market hours; without this, the idea funnel's wall-clock budget
# expires and the insider signal silently drops to empty - closing the 2-of-3
# promotion path exactly when it would be most useful.
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "form4_cache.json"
_CACHE_TTL_S = 24 * 3600


def _headers() -> dict:
    return {**_HEADERS, "User-Agent": settings.sec_user_agent}


def _load_cache() -> dict:
    """Load the form 4 cache. Missing or malformed file -> empty dict."""
    if not _CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_CACHE_PATH.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass


def _entry_age_s(entry: dict) -> float | None:
    """Age in seconds of a cache entry, or None if its timestamp is unusable."""
    try:
        dt = datetime.fromisoformat(entry.get("fetched_at") or "")
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _cached(key: str, fetcher):
    """Disk-cache wrapper. ``fetcher`` returns ``(data, ok)`` where ``ok`` is
    False on a genuine fetch failure (network/parse) - distinct from a valid
    empty result. A fresh entry (< 24h) is served without calling ``fetcher``;
    on a fetch failure a stale entry is served if one exists.
    """
    cache = _load_cache()
    entry = cache.get(key)
    if not (isinstance(entry, dict) and "data" in entry):
        entry = None
    elif _entry_age_s(entry) is not None and _entry_age_s(entry) < _CACHE_TTL_S:
        return entry["data"]
    data, ok = fetcher()
    if ok:
        cache[key] = {"fetched_at": datetime.now(timezone.utc).isoformat(),
                      "data": data}
        _save_cache(cache)
        return data
    if entry is not None:
        log.warning("form4 cache: stale-served %s (live fetch failed)", key)
        return entry["data"]
    return data


def recent_form4(ticker: str, days: int = 30) -> dict:
    """Return a summary of Form 4 filings in the lookback window (count only).

    24h disk-cached; a fetch failure serves a stale entry when one exists.
    """
    key = f"{ticker.upper()}|{days}|recent_form4"

    def _fetch():
        result = _recent_form4_uncached(ticker, days)
        return result, not bool(result.get("error"))

    return _cached(key, _fetch)


def _recent_form4_uncached(ticker: str, days: int = 30) -> dict:
    """Live Form 4 filing-count fetch (no cache)."""
    try:
        mapping = _ticker_to_cik()
    except CIKLookupError as e:
        LAST_FETCH_ERRORS[ticker.upper()] = str(e)
        return {"count": 0, "filings": [], "error": str(e),
                 "data_available": False}
    except Exception as e:
        msg = f"cik lookup failed: {type(e).__name__}: {e}"
        LAST_FETCH_ERRORS[ticker.upper()] = msg
        return {"count": 0, "filings": [], "error": msg,
                 "data_available": False}
    cik = mapping.get(ticker.upper())
    if not cik:
        # Legitimately not in the index (e.g. ETFs, foreign issuers).
        # NOT a data-availability problem.
        LAST_FETCH_ERRORS.pop(ticker.upper(), None)
        return {"count": 0, "filings": [],
                 "error": "ticker not in CIK index",
                 "data_available": True}
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
        filings.append({
            "form": form,
            "filed": filed_dates[i],
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=10",
            "accession": accessions[i],
        })
        if len(filings) >= 20:
            break
    return {"count": len(filings), "filings": filings, "lookback_days": days}


# ---------------------------------------------------------------------------
# Form 4 XML parsing
# ---------------------------------------------------------------------------

def _parse_float(el) -> float | None:
    if el is None or not el.text:
        return None
    try:
        return float(el.text.strip())
    except ValueError:
        return None


def _parse_form4_xml(xml_text: str) -> list[dict]:
    """Parse a Form 4 ownership XML document into transaction dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    name_el = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
    owner = (name_el.text or "").strip() if (name_el is not None and name_el.text) else ""
    title_el = root.find(".//reportingOwner/reportingOwnerRelationship/officerTitle")
    role = (title_el.text or "").strip() if (title_el is not None and title_el.text) else ""

    # 10b5-1 detection from footnote text (free, deterministic heuristic).
    planned = False
    for fn in root.findall(".//footnotes/footnote"):
        txt = (fn.text or "").lower()
        if "10b5-1" in txt or "10b5–1" in txt or "rule 10b5" in txt:
            planned = True
            break

    out: list[dict] = []
    for t in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code_el = t.find("transactionCoding/transactionCode")
        code = (code_el.text or "").strip() if (code_el is not None and code_el.text) else ""
        date_el = t.find("transactionDate/value")
        tdate = (date_el.text or "").strip() if (date_el is not None and date_el.text) else ""
        shares = _parse_float(t.find("transactionAmounts/transactionShares/value"))
        price = _parse_float(t.find("transactionAmounts/transactionPricePerShare/value"))
        ad_el = t.find("transactionAmounts/transactionAcquiredDisposedCode/value")
        ad = (ad_el.text or "").strip() if (ad_el is not None and ad_el.text) else ""
        out.append({
            "filer_name": owner,
            "role": role,
            "transaction_date": tdate,
            "transaction_code": code,
            "acquired_disposed": ad,
            "shares": shares,
            "price": price,
            "total_value": round((shares or 0.0) * (price or 0.0), 2),
            "is_planned_10b5_1": planned,
        })
    return out


def _form4_transactions_cached(cik_int: str, accession: str, primary_doc: str) -> list[dict]:
    """Fetch + parse one Form 4's transactions, cached on disk by accession."""
    _FORM4_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = _FORM4_CACHE / f"{accession}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    acc_nodash = accession.replace("-", "")
    candidates: list[str] = []
    if primary_doc and primary_doc.lower().endswith(".xml"):
        candidates.append(primary_doc)
    candidates.append(f"{accession}.xml")
    txns: list[dict] = []
    for doc in candidates:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
        try:
            r = httpx.get(url, headers=_headers(), timeout=_TIMEOUT)
        except Exception as e:
            log.debug("form4 fetch failed %s: %s", url, e)
            continue
        if r.status_code == 200 and "<ownershipDocument" in r.text:
            txns = _parse_form4_xml(r.text)
            break
    try:
        cache_file.write_text(json.dumps(txns))
    except Exception:
        pass
    return txns


def recent_form4_transactions(ticker: str, days: int = 30,
                              max_filings: int = 40) -> list[dict]:
    """Return parsed Form 4 transactions for ``ticker`` within ``days``.

    Each dict has: filer_name, role, transaction_date, transaction_code,
    acquired_disposed, shares, price, total_value, is_planned_10b5_1.
    Transactions are filtered so their ``transaction_date`` falls within the
    window (a filing dated inside the window can report an older trade).

    24h disk-cached; a fetch failure serves a stale entry when one exists.
    """
    key = f"{ticker.upper()}|{days}|recent_form4_transactions"
    return _cached(key, lambda: _recent_form4_transactions_uncached(
        ticker, days, max_filings))


def _recent_form4_transactions_uncached(ticker: str, days: int = 30,
                                        max_filings: int = 40
                                        ) -> tuple[list[dict], bool]:
    """Live Form 4 transaction fetch (no cache).

    Returns ``(transactions, ok)``. ``ok`` is False only on a genuine fetch
    failure (CIK lookup or submissions request) - a ticker that simply is not
    in the CIK index is a valid empty result and is cacheable.

    On failure, records the reason in :data:`LAST_FETCH_ERRORS` so callers
    can render a loud "insider data unavailable: <reason>" diagnostic
    instead of treating the empty list as a genuine score-0.
    """
    try:
        mapping = _ticker_to_cik()
    except CIKLookupError as e:
        LAST_FETCH_ERRORS[ticker.upper()] = str(e)
        return [], False
    except Exception as e:
        LAST_FETCH_ERRORS[ticker.upper()] = (
            f"cik lookup failed: {type(e).__name__}: {e}")
        return [], False
    cik = mapping.get(ticker.upper())
    if not cik:
        # Legitimately absent from the index (ETFs, foreign issuers, ...).
        # Not a data-availability problem.
        LAST_FETCH_ERRORS.pop(ticker.upper(), None)
        return [], True
    try:
        r = httpx.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=_headers(), timeout=_TIMEOUT,
        )
        r.raise_for_status()
        sub = r.json()
    except Exception as e:
        msg = f"submissions fetch failed: {type(e).__name__}: {e}"
        log.warning("form4 submissions fetch failed for %s: %s", ticker, e)
        LAST_FETCH_ERRORS[ticker.upper()] = msg
        return [], False
    # Success - clear any stale error for this ticker.
    LAST_FETCH_ERRORS.pop(ticker.upper(), None)
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filed = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    primary = recent.get("primaryDocument", [])
    cutoff = date.today() - timedelta(days=days)
    cik_int = str(int(cik))

    collected: list[dict] = []
    seen = 0
    for i, form in enumerate(forms):
        if form not in ("4", "4/A"):
            continue
        try:
            d = date.fromisoformat(filed[i])
        except Exception:
            continue
        if d < cutoff:
            continue
        seen += 1
        if seen > max_filings:
            break
        doc = primary[i] if i < len(primary) else ""
        collected.extend(_form4_transactions_cached(cik_int, accs[i], doc))

    result: list[dict] = []
    for t in collected:
        td = t.get("transaction_date")
        try:
            if td and date.fromisoformat(td) >= cutoff:
                result.append(t)
        except Exception:
            result.append(t)  # keep unparseable dates rather than silently drop
    return result, True


def diagnostics() -> dict:
    """Snapshot of the SEC fetch state.

    ``cik_index_error`` is the most recent CIK-index fetch failure
    (``None`` if the index resolved cleanly on the last attempt).
    ``ticker_errors`` is a dict of {ticker: reason} from the most recent
    insider lookup per ticker (empty if everything went OK).

    Used by ``build_site.py`` to surface "insider data unavailable" on
    the today page instead of letting a fetch failure silently masquerade
    as score 0.
    """
    return {
        "cik_index_error": filings.LAST_CIK_ERROR,
        "ticker_errors": dict(LAST_FETCH_ERRORS),
        "tickers_unavailable": len(LAST_FETCH_ERRORS),
    }


def reset_diagnostics() -> None:
    """Clear the per-ticker error record. Used between builds (and tests)."""
    LAST_FETCH_ERRORS.clear()
