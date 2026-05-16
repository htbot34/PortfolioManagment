"""Price + fundamentals fetcher backed by yfinance.

All calls are wrapped in try/except so a single failed ticker never crashes the
dashboard. Results for the dashboard are cached in-process for 60s.
"""
from dataclasses import dataclass, asdict
from time import time

import yfinance as yf


@dataclass
class Quote:
    ticker: str
    price: float | None
    prev_close: float | None
    day_change_pct: float | None
    market_cap: float | None
    pe_ratio: float | None
    high_52w: float | None
    low_52w: float | None
    sector: str | None
    industry: str | None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_CACHE: dict[str, tuple[float, Quote]] = {}
_TTL_S = 60.0


def quote(ticker: str) -> Quote:
    ticker = ticker.upper()
    now = time()
    cached = _CACHE.get(ticker)
    if cached and now - cached[0] < _TTL_S:
        return cached[1]
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
        day_change_pct = None
        if price is not None and prev:
            day_change_pct = (price - prev) / prev * 100
        q = Quote(
            ticker=ticker,
            price=float(price) if price is not None else None,
            prev_close=float(prev) if prev is not None else None,
            day_change_pct=day_change_pct,
            market_cap=info.get("marketCap"),
            pe_ratio=info.get("trailingPE"),
            high_52w=info.get("fiftyTwoWeekHigh"),
            low_52w=info.get("fiftyTwoWeekLow"),
            sector=info.get("sector"),
            industry=info.get("industry"),
        )
    except Exception as e:
        q = Quote(
            ticker=ticker, price=None, prev_close=None, day_change_pct=None,
            market_cap=None, pe_ratio=None, high_52w=None, low_52w=None,
            sector=None, industry=None, error=str(e),
        )
    _CACHE[ticker] = (now, q)
    return q


def quotes(tickers: list[str]) -> dict[str, Quote]:
    return {t.upper(): quote(t) for t in tickers}


def history(ticker: str, period: str = "6mo"):
    """Return a pandas DataFrame of OHLCV history, or None on failure."""
    try:
        return yf.Ticker(ticker).history(period=period, auto_adjust=True)
    except Exception:
        return None


def technicals(ticker: str) -> dict:
    """Compute a small bundle of swing/long-term technicals from 1y of prices."""
    df = history(ticker, period="1y")
    if df is None or df.empty:
        return {"error": "no price history"}
    close = df["Close"]
    last = float(close.iloc[-1])
    sma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
    sma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    high_52w = float(close.max())
    low_52w = float(close.min())
    pct_off_high = (last - high_52w) / high_52w * 100 if high_52w else None

    # Simple 14-day RSI
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    down = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / down.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    rsi_last = float(rsi.iloc[-1]) if not rsi.empty else None

    return {
        "last": last,
        "sma50": sma50,
        "sma200": sma200,
        "above_sma50": (last > sma50) if sma50 else None,
        "above_sma200": (last > sma200) if sma200 else None,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pct_off_52w_high": pct_off_high,
        "rsi14": rsi_last,
    }
