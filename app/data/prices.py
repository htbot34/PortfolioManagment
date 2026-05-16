"""Price + fundamentals fetcher.

Provider chain:
  1) Stooq (free, no key, reliable from cloud IPs) for daily OHLCV
  2) yfinance (best effort) for fundamentals: sector, industry, P/E, market cap

If Stooq fails, we try yfinance for prices too. All errors degrade gracefully -
the dashboard simply shows '-' for any missing field.
"""
import io
from dataclasses import dataclass, asdict
from time import time

import httpx
import pandas as pd


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


_HISTORY_CACHE: dict[str, tuple[float, pd.DataFrame | None]] = {}
_QUOTE_CACHE: dict[str, tuple[float, Quote]] = {}
_TTL_S = 300.0


def _stooq_history(ticker: str) -> pd.DataFrame | None:
    cached = _HISTORY_CACHE.get(ticker)
    now = time()
    if cached and now - cached[0] < _TTL_S:
        return cached[1]
    df: pd.DataFrame | None = None
    try:
        r = httpx.get(
            f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d",
            headers={"User-Agent": "Mozilla/5.0 (compatible; PortfolioAdvisor/1.0)"},
            timeout=20,
        )
        if r.status_code == 200 and r.text and "No data" not in r.text[:50]:
            tmp = pd.read_csv(io.StringIO(r.text))
            if not tmp.empty and "Close" in tmp.columns:
                tmp["Date"] = pd.to_datetime(tmp["Date"])
                df = tmp.set_index("Date").tail(252)
    except Exception:
        df = None
    _HISTORY_CACHE[ticker] = (now, df)
    return df


def _yfinance_history(ticker: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
        return df if df is not None and not df.empty else None
    except Exception:
        return None


def _history(ticker: str) -> pd.DataFrame | None:
    df = _stooq_history(ticker)
    if df is not None and not df.empty:
        return df
    return _yfinance_history(ticker)


def _yfinance_fundamentals(ticker: str) -> dict:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return {}
    return {
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
    }


def quote(ticker: str) -> Quote:
    ticker = ticker.upper()
    now = time()
    cached = _QUOTE_CACHE.get(ticker)
    if cached and now - cached[0] < _TTL_S:
        return cached[1]

    df = _history(ticker)
    price = prev_close = day_change_pct = high_52w = low_52w = None
    error = None
    if df is not None and not df.empty:
        close = df["Close"]
        price = float(close.iloc[-1])
        if len(close) >= 2:
            prev_close = float(close.iloc[-2])
            day_change_pct = (price - prev_close) / prev_close * 100 if prev_close else None
        high_52w = float(close.max())
        low_52w = float(close.min())
    else:
        error = "no price data (Stooq + yfinance both failed)"

    fund = _yfinance_fundamentals(ticker)
    q = Quote(
        ticker=ticker, price=price, prev_close=prev_close, day_change_pct=day_change_pct,
        market_cap=fund.get("market_cap"), pe_ratio=fund.get("pe_ratio"),
        high_52w=high_52w, low_52w=low_52w,
        sector=fund.get("sector"), industry=fund.get("industry"),
        error=error,
    )
    _QUOTE_CACHE[ticker] = (now, q)
    return q


def quotes(tickers: list[str]) -> dict[str, Quote]:
    return {t.upper(): quote(t) for t in tickers}


def history(ticker: str, period: str = "6mo"):
    return _history(ticker)


def technicals(ticker: str) -> dict:
    df = _history(ticker)
    if df is None or df.empty:
        return {"error": "no price history"}
    close = df["Close"]
    last = float(close.iloc[-1])
    sma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
    sma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    high_52w = float(close.max())
    low_52w = float(close.min())
    pct_off_high = (last - high_52w) / high_52w * 100 if high_52w else None

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
