"""Price + fundamentals fetcher with multi-source fallback.

Provider chain (each wrapped, never raises):
  1) Stooq daily CSV   - free, no key
  2) Yahoo chart API   - direct HTTP, no yfinance overhead (often works when
                         yfinance's full client doesn't)
  3) yfinance          - last resort, also pulls fundamentals (sector, P/E)

If everything fails, error fields carry the diagnosis so the failure is visible
in the rendered site and in data.json.
"""
import io
import logging
from dataclasses import dataclass, asdict
from time import time

import httpx
import pandas as pd

log = logging.getLogger("prices")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"


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
    source: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_HISTORY_CACHE: dict[str, tuple[float, pd.DataFrame | None, str | None]] = {}
_TTL_S = 300.0


def _try_stooq(ticker: str) -> pd.DataFrame | None:
    try:
        r = httpx.get(
            f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d",
            headers={"User-Agent": _UA},
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code != 200 or not r.text or r.text[:50].lower().startswith("no data"):
            return None
        df = pd.read_csv(io.StringIO(r.text))
        if df.empty or "Close" not in df.columns:
            return None
        df["Date"] = pd.to_datetime(df["Date"])
        return df.set_index("Date").tail(252)
    except Exception as e:
        log.debug("stooq failed for %s: %s", ticker, e)
        return None


def _try_yahoo_chart(ticker: str) -> pd.DataFrame | None:
    try:
        r = httpx.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "1y"},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        item = result[0]
        ts = item.get("timestamp") or []
        ind = ((item.get("indicators") or {}).get("quote") or [{}])[0]
        closes = ind.get("close")
        if not ts or not closes:
            return None
        df = pd.DataFrame({
            "Date": pd.to_datetime(ts, unit="s"),
            "Open": ind.get("open"),
            "High": ind.get("high"),
            "Low": ind.get("low"),
            "Close": closes,
            "Volume": ind.get("volume"),
        }).dropna(subset=["Close"]).set_index("Date")
        return df if not df.empty else None
    except Exception as e:
        log.debug("yahoo chart failed for %s: %s", ticker, e)
        return None


def _try_yfinance(ticker: str) -> pd.DataFrame | None:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
        return df if df is not None and not df.empty else None
    except Exception as e:
        log.debug("yfinance failed for %s: %s", ticker, e)
        return None


_SOURCES = [
    ("stooq", _try_stooq),
    ("yahoo_chart", _try_yahoo_chart),
    ("yfinance", _try_yfinance),
]


def _history_with_source(ticker: str) -> tuple[pd.DataFrame | None, str | None]:
    cached = _HISTORY_CACHE.get(ticker)
    now = time()
    if cached and now - cached[0] < _TTL_S:
        return cached[1], cached[2]
    for name, fn in _SOURCES:
        df = fn(ticker)
        if df is not None and not df.empty:
            _HISTORY_CACHE[ticker] = (now, df, name)
            return df, name
    _HISTORY_CACHE[ticker] = (now, None, None)
    return None, None


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
    df, source = _history_with_source(ticker)
    price = prev_close = day_change_pct = high_52w = low_52w = None
    error = None
    if df is not None and not df.empty:
        close = df["Close"]
        price = float(close.iloc[-1])
        if len(close) >= 2:
            prev_close = float(close.iloc[-2])
            if prev_close:
                day_change_pct = (price - prev_close) / prev_close * 100
        high_52w = float(close.max())
        low_52w = float(close.min())
    else:
        error = f"No price source returned data (tried: {', '.join(s for s, _ in _SOURCES)})"

    fund = _yfinance_fundamentals(ticker)
    return Quote(
        ticker=ticker, price=price, prev_close=prev_close, day_change_pct=day_change_pct,
        market_cap=fund.get("market_cap"), pe_ratio=fund.get("pe_ratio"),
        high_52w=high_52w, low_52w=low_52w,
        sector=fund.get("sector"), industry=fund.get("industry"),
        source=source, error=error,
    )


def quotes(tickers: list[str]) -> dict[str, Quote]:
    return {t.upper(): quote(t) for t in tickers}


def history(ticker: str, period: str = "6mo") -> pd.DataFrame | None:
    df, _ = _history_with_source(ticker)
    return df


def technicals(ticker: str) -> dict:
    df, _ = _history_with_source(ticker)
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


def diagnose(ticker: str = "META") -> dict:
    """Probe each source individually and report what worked. Used at workflow start."""
    out: dict[str, str] = {}
    for name, fn in _SOURCES:
        try:
            df = fn(ticker)
            out[name] = f"ok ({len(df)} rows, last close ${float(df['Close'].iloc[-1]):.2f})" if df is not None and not df.empty else "no data"
        except Exception as e:
            out[name] = f"error: {e}"
    return out
