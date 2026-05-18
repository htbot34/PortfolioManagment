"""Macro market context: indices, volatility, rates, sector ETFs.

Uses the same multi-source price chain as positions. All errors degrade
gracefully - missing fields render as '-' in the brief. Sector ETF entries
also carry 5d and 20d simple returns so the conviction gate can read sector
momentum without re-fetching history.
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


def _period_returns(ticker: str) -> tuple[float | None, float | None]:
    """Return (5d_pct, 20d_pct) simple returns. None if history insufficient."""
    df = prices.history(ticker)
    if df is None or df.empty or "Close" not in df.columns:
        return None, None
    close = df["Close"]
    if len(close) < 21:
        return None, None
    last = float(close.iloc[-1])
    five = float(close.iloc[-6])
    twenty = float(close.iloc[-21])
    r5 = (last - five) / five * 100 if five else None
    r20 = (last - twenty) / twenty * 100 if twenty else None
    return r5, r20


def _short(ticker: str, with_period_returns: bool = False) -> dict:
    # Indices and sector ETFs don't need fundamentals - just price and day
    # change. fast=True skips the slow yfinance fundamentals fetch.
    q = prices.quote(ticker, fast=True)
    out = {
        "ticker": ticker,
        "price": q.price,
        "day_change_pct": q.day_change_pct,
        "high_52w": q.high_52w,
        "low_52w": q.low_52w,
        "pct_off_52w_high": ((q.price - q.high_52w) / q.high_52w * 100) if q.price and q.high_52w else None,
    }
    if with_period_returns:
        r5, r20 = _period_returns(ticker)
        out["ret_5d"] = r5
        out["ret_20d"] = r20
    return out


def snapshot() -> dict:
    """Return a compact macro snapshot for the morning brief."""
    indices = {label: _short(symbol) for label, symbol in INDICES.items()}
    sectors = {label: _short(symbol, with_period_returns=True)
               for label, symbol in SECTOR_ETFS.items()}

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

