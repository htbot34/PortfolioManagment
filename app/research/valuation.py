"""Valuation overlay - sector-relative percentile.

A recommendation that has already cleared the conviction gate gets a
valuation tier check: if the name is at an extreme valuation versus its
sector peers we downgrade it; if it's cheap we flag a sizing tailwind.

``valuation_score`` is the pure scorer. ``build_sector_comparables`` pulls
peer fundamentals (cached per process for the run).

Percentile is computed on the first available metric of trailing P/E ->
forward P/E -> price/sales, using the SAME metric for the candidate and
its peers. 100 = most expensive in the sector sample.

``vs_own_history_percentile`` is intentionally left as None: a real
historical-P/E percentile needs a quarterly EPS time series we do not
store, and the spec permits skipping it.
"""
from __future__ import annotations

from typing import Callable

from app.logging import get_logger

log = get_logger(__name__)

# Static sector -> representative large-cap peers. ~12 per sector is enough
# for a stable percentile. Refresh manually if a sector's makeup drifts.
SECTOR_PEERS: dict[str, list[str]] = {
    "Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD",
                    "ADBE", "CSCO", "ACN", "TXN", "QCOM", "INTU", "NOW"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "TMUS",
                                "VZ", "T", "CMCSA", "EA", "TTWO"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX",
                           "BKNG", "TJX", "ABNB", "MAR", "ORLY"],
    "Consumer Defensive": ["WMT", "COST", "PG", "KO", "PEP", "PM", "MDLZ",
                            "CL", "MO", "TGT", "KMB", "GIS"],
    "Financial Services": ["JPM", "V", "MA", "BAC", "WFC", "GS", "MS",
                            "SPGI", "AXP", "BLK", "C", "SCHW"],
    "Healthcare": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "PFE",
                    "DHR", "ISRG", "AMGN", "VRTX"],
    "Energy": ["XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "WMB",
                "OKE", "VLO", "HES", "DVN"],
    "Industrials": ["GE", "CAT", "RTX", "HON", "UNP", "ETN", "BA", "DE",
                     "LMT", "UPS", "PH", "TT"],
    "Basic Materials": ["LIN", "SHW", "FCX", "ECL", "APD", "NEM", "DOW",
                         "NUE", "CTVA", "DD", "PPG", "VMC"],
    "Utilities": ["NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "EXC",
                   "XEL", "ED", "PEG", "EIX"],
    "Real Estate": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI",
                     "DLR", "CBRE", "VICI", "EXR"],
}

_METRIC_PRIORITY = ("trailing_pe", "forward_pe", "price_to_sales")
_MIN_PEERS = 3

# Per-process memo so 8 held tickers in the same sector don't rebuild.
_COMPARABLE_CACHE: dict[str, list[dict]] = {}


def _pick_metric(fundamentals: dict) -> tuple[str | None, float | None]:
    """Choose the first usable (positive) valuation metric."""
    for m in _METRIC_PRIORITY:
        v = (fundamentals or {}).get(m)
        if isinstance(v, (int, float)) and v > 0:
            return m, float(v)
    return None, None


def build_sector_comparables(sector: str | None,
                             fundamentals_getter: Callable | None = None) -> list[dict]:
    """Return a list of peer fundamentals dicts for ``sector``.

    Memoized per process. Returns [] for an unknown sector.
    """
    if not sector or sector not in SECTOR_PEERS:
        return []
    if sector in _COMPARABLE_CACHE:
        return _COMPARABLE_CACHE[sector]
    if fundamentals_getter is None:
        from app.data.fundamentals import get_fundamentals
        fundamentals_getter = get_fundamentals
    out: list[dict] = []
    for peer in SECTOR_PEERS[sector]:
        try:
            out.append(fundamentals_getter(peer))
        except Exception as e:
            log.debug("comparable fetch failed for %s: %s", peer, e)
    _COMPARABLE_CACHE[sector] = out
    return out


def valuation_score(ticker: str, fundamentals: dict,
                    sector_comparables: list[dict]) -> dict:
    """Score ``ticker``'s valuation against its sector peers.

    Returns: tier (cheap/fair/expensive/extreme/unknown),
    percentile_in_sector (0-100, 100 = most expensive), metric used,
    metric_value, vs_own_history_percentile (None - see module docstring),
    n_peers, summary.
    """
    unknown = {
        "tier": "unknown", "percentile_in_sector": None,
        "vs_own_history_percentile": None, "metric": None,
        "metric_value": None, "n_peers": 0,
        "summary": "valuation unknown (insufficient data)",
    }
    metric, value = _pick_metric(fundamentals)
    if metric is None or not sector_comparables:
        return unknown
    peer_values = [
        c.get(metric) for c in sector_comparables
        if isinstance(c.get(metric), (int, float)) and c.get(metric) > 0
        and (c.get("ticker") or "").upper() != ticker.upper()
    ]
    if len(peer_values) < _MIN_PEERS:
        return unknown
    below = sum(1 for v in peer_values if v <= value)
    pct = round(below / len(peer_values) * 100, 1)
    tier = ("cheap" if pct < 25 else
            "fair" if pct < 75 else
            "expensive" if pct < 90 else
            "extreme")
    return {
        "tier": tier,
        "percentile_in_sector": pct,
        "vs_own_history_percentile": None,
        "metric": metric,
        "metric_value": round(value, 2),
        "n_peers": len(peer_values),
        "summary": (f"{metric.replace('_', ' ')} {value:.1f} = "
                    f"{pct:.0f}th percentile in sector ({tier})"),
    }
