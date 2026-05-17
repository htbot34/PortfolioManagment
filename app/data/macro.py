"""Macro market context: indices, volatility, rates, sector ETFs.

Uses the same multi-source price chain as positions. All errors degrade
gracefully - missing fields render as '-' in the brief.
"""
from app.data import prices

INDICES = {
    "SPX": "^GSPC",      # S&P 500
    "NDX": "^NDX",       # Nasdaq 100
    "RUT": "^RUT",       # Russell 2000
    "VIX": "^VIX",       # CBOE volatility
    "DXY": "DX-Y.NYB",   # Dollar index
    "TNX": "^TNX",       # 10-year Treasury yield (x10)
}

SECTOR_ETFS = {
    "Tech": "XLK",
    "Comm": "XLC",
    "Cons Disc": "XLY",
    "Cons Stap": "XLP",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def _short(ticker: str) -> dict:
    q = prices.quote(ticker)
    return {
        "ticker": ticker,
        "price": q.price,
        "day_change_pct": q.day_change_pct,
        "high_52w": q.high_52w,
        "low_52w": q.low_52w,
        "pct_off_52w_high": ((q.price - q.high_52w) / q.high_52w * 100) if q.price and q.high_52w else None,
    }


def snapshot() -> dict:
    """Return a compact macro snapshot for the morning brief."""
    indices = {label: _short(symbol) for label, symbol in INDICES.items()}
    sectors = {label: _short(symbol) for label, symbol in SECTOR_ETFS.items()}

    sorted_sectors = sorted(
        ((label, data) for label, data in sectors.items() if data["day_change_pct"] is not None),
        key=lambda kv: kv[1]["day_change_pct"], reverse=True,
    )
    leaders = sorted_sectors[:3]
    laggards = sorted_sectors[-3:]

    return {
        "indices": indices,
        "sectors": sectors,
        "leaders": [{"name": l, **d} for l, d in leaders],
        "laggards": [{"name": l, **d} for l, d in laggards],
    }
