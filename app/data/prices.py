"""Price + fundamentals fetcher with multi-source fallback.

Provider chain (each wrapped, never raises):
  1) Stooq daily CSV   - free, no key
  2) Yahoo chart API   - direct HTTP, no yfinance overhead (often works when
                         yfinance's full client doesn't)
  3) yfinance          - last resort, also pulls fundamentals (sector, P/E)
  4) Persistent disk cache - prices from last successful run, so the brief
                              still computes when all live sources fail.

If everything fails, error fields carry the diagnosis so the failure is visible
in the rendered site and in data.json.
"""
import io
import json
import time as _time
from dataclasses import dataclass, asdict
from functools import wraps
from pathlib import Path
from time import time

import httpx
import pandas as pd

from app.config import settings
from app.logging import get_logger

_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "price_cache.json"

log = get_logger(__name__)


def _retry(attempts: int = 3, backoff_s: float = 0.5):
    """Retry decorator with linear backoff.

    Only retries on exceptions (transient errors like timeouts or 5xx).
    A ``None`` return is treated as a legitimate "no data" signal and is
    NOT retried - that path is for things like 403/404/empty bodies where
    retrying just wastes time.
    Never raises; the worst case is a final ``None`` after the last attempt.
    """
    def deco(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            last_err: Exception | None = None
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    log.debug("%s attempt %d/%d raised: %s", fn.__name__, i + 1, attempts, e)
                    if i < attempts - 1:
                        _time.sleep(backoff_s * (i + 1))
            if last_err is not None:
                log.warning("%s exhausted retries (%d): %s", fn.__name__, attempts, last_err)
            return None
        return inner
    return deco

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


@_retry(attempts=3, backoff_s=0.5)
def _try_stooq(ticker: str) -> pd.DataFrame | None:
    r = httpx.get(
        f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d",
        headers={"User-Agent": _UA},
        timeout=8,
        follow_redirects=True,
    )
    if r.status_code != 200 or not r.text or r.text[:50].lower().startswith("no data"):
        return None
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty or "Close" not in df.columns:
        return None
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").tail(252)


@_retry(attempts=3, backoff_s=0.5)
def _try_yahoo_chart(ticker: str) -> pd.DataFrame | None:
    r = httpx.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"interval": "1d", "range": "1y"},
        headers={"User-Agent": _UA, "Accept": "application/json"},
        timeout=8,
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


@_retry(attempts=2, backoff_s=0.5)
def _try_yfinance(ticker: str) -> pd.DataFrame | None:
    """Last-resort price source. ``_try_yahoo_chart`` already hits the same
    chart endpoint with less overhead, so this only runs when both Stooq
    and the direct chart call returned no data. No fast_info fallback -
    that path turned out to add latency without rescuing any tickers.
    """
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
    return df if df is not None and not df.empty else None


_SOURCES = [
    ("stooq", _try_stooq),
    ("yahoo_chart", _try_yahoo_chart),
    ("yfinance", _try_yfinance),
]


def _load_persistent_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_persistent_cache(data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, default=str))
    except Exception:
        pass


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


def _from_persistent_cache(ticker: str) -> dict | None:
    """Return cached quote dict if available."""
    cache = _load_persistent_cache()
    entry = cache.get(ticker.upper())
    if not entry:
        return None
    return entry


def _save_to_persistent_cache(ticker: str, payload: dict) -> None:
    cache = _load_persistent_cache()
    cache[ticker.upper()] = payload
    _save_persistent_cache(cache)


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


def quote(ticker: str, fast: bool = False) -> Quote:
    """Fetch a quote for ``ticker``.

    Set ``fast=True`` to skip the yfinance fundamentals call (sector,
    industry, market_cap, P/E). The fundamentals call hits ``Ticker.info``
    which does Yahoo's crumb-cookie dance and is the single slowest piece
    of the pipeline (often 5-30 seconds per ticker under throttling). The
    scanner and macro snapshot don't need fundamentals, so they pass
    ``fast=True`` to keep total runtime sane.
    """
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

    fund = {} if fast else _yfinance_fundamentals(ticker)
    q = Quote(
        ticker=ticker, price=price, prev_close=prev_close, day_change_pct=day_change_pct,
        market_cap=fund.get("market_cap"), pe_ratio=fund.get("pe_ratio"),
        high_52w=high_52w, low_52w=low_52w,
        sector=fund.get("sector"), industry=fund.get("industry"),
        source=source, error=error,
    )
    if price is not None:
        # Persist the live fetch for future fallback use.
        _save_to_persistent_cache(ticker, q.to_dict())
        return q
    # Live fetch failed for every source - fall back to persistent cache.
    cached = _from_persistent_cache(ticker)
    if cached and cached.get("price") is not None:
        return Quote(
            ticker=ticker,
            price=cached.get("price"),
            prev_close=cached.get("prev_close"),
            day_change_pct=None,
            market_cap=cached.get("market_cap"),
            pe_ratio=cached.get("pe_ratio"),
            high_52w=cached.get("high_52w"),
            low_52w=cached.get("low_52w"),
            sector=cached.get("sector"),
            industry=cached.get("industry"),
            source="cache",
            error="using cached price (live sources failed)",
        )
    return q


def quotes(tickers: list[str], fast: bool = False) -> dict[str, Quote]:
    return {t.upper(): quote(t, fast=fast) for t in tickers}


def history(ticker: str, period: str = "6mo") -> pd.DataFrame | None:
    df, _ = _history_with_source(ticker)
    return df


_EMPTY_TECHNICALS = {
    "last": None, "sma20": None, "sma50": None, "sma200": None,
    "sma50_series": None, "sma200_series": None,
    "above_sma50": None, "above_sma200": None,
    "stacked_uptrend": None, "stacked_downtrend": None,
    "golden_cross_recent": None, "death_cross_recent": None,
    "high_52w": None, "low_52w": None, "pct_off_52w_high": None,
    "rsi14": None, "macd_hist": None, "macd_cross_up": None, "macd_cross_down": None,
    "bb_upper": None, "bb_lower": None, "bb_pct": None,
    "atr14": None, "atr_pct": None, "vol_ratio_20d": None, "breakout_20d": None,
}


def _wilder_rsi(close: "pd.Series", period: int = 14) -> "pd.Series":
    """RSI using Wilder's smoothing (matches most charting platforms).

    Initial average gain/loss is a simple mean of the first ``period`` deltas;
    subsequent values are smoothed with ``avg = (prev*(N-1) + new) / N``.
    """
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = (-delta.clip(upper=0))
    if len(close) < period + 1:
        return pd.Series([None] * len(close), index=close.index)
    avg_gain = gains.copy() * 0.0
    avg_loss = losses.copy() * 0.0
    avg_gain.iloc[period] = gains.iloc[1 : period + 1].mean()
    avg_loss.iloc[period] = losses.iloc[1 : period + 1].mean()
    for i in range(period + 1, len(close)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gains.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + losses.iloc[i]) / period
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _series_dump(s: "pd.Series", tail: int = 200) -> list[dict]:
    """Convert a price/indicator series to a JSON-friendly list of points."""
    s = s.dropna().tail(tail)
    out = []
    for ts, v in s.items():
        try:
            date_str = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
        except Exception:
            date_str = str(ts)[:10]
        out.append({"date": date_str, "value": float(v)})
    return out


def technicals(ticker: str) -> dict:
    df, _ = _history_with_source(ticker)
    if df is None or df.empty:
        return {**_EMPTY_TECHNICALS, "error": "no price history"}
    close = df["Close"]
    high = df["High"] if "High" in df.columns else close
    low = df["Low"] if "Low" in df.columns else close
    volume = df["Volume"] if "Volume" in df.columns else None
    last = float(close.iloc[-1])
    sma20_series = close.rolling(20).mean() if len(close) >= 20 else pd.Series(dtype=float)
    sma50_series = close.rolling(50).mean() if len(close) >= 50 else pd.Series(dtype=float)
    sma200_series = close.rolling(200).mean() if len(close) >= 200 else pd.Series(dtype=float)
    sma20 = float(sma20_series.iloc[-1]) if not sma20_series.dropna().empty else None
    sma50 = float(sma50_series.iloc[-1]) if not sma50_series.dropna().empty else None
    sma200 = float(sma200_series.iloc[-1]) if not sma200_series.dropna().empty else None
    high_52w = float(close.max())
    low_52w = float(close.min())
    high_20d = float(close.tail(20).max()) if len(close) >= 20 else None
    pct_off_high = (last - high_52w) / high_52w * 100 if high_52w else None

    # Golden / death cross detection within last 20 trading days.
    golden_cross_recent = False
    death_cross_recent = False
    if not sma50_series.dropna().empty and not sma200_series.dropna().empty and len(close) > 200:
        sig = (sma50_series - sma200_series).dropna().tail(21)
        if len(sig) >= 2:
            prev = sig.shift(1).dropna()
            cur = sig.iloc[1:]
            crosses_up = ((prev <= 0) & (cur > 0)).sum()
            crosses_dn = ((prev >= 0) & (cur < 0)).sum()
            golden_cross_recent = bool(crosses_up > 0)
            death_cross_recent = bool(crosses_dn > 0)

    rsi_series = _wilder_rsi(close, 14)
    rsi_last = float(rsi_series.iloc[-1]) if not rsi_series.dropna().empty else None

    # MACD (12, 26, 9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line
    macd_cross_up = bool(len(macd_hist) >= 2 and macd_hist.iloc[-2] < 0 < macd_hist.iloc[-1])
    macd_cross_down = bool(len(macd_hist) >= 2 and macd_hist.iloc[-2] > 0 > macd_hist.iloc[-1])

    # Bollinger Bands (20, 2)
    if len(close) >= 20:
        ma20 = close.tail(20).mean()
        sd20 = close.tail(20).std()
        bb_upper = float(ma20 + 2 * sd20)
        bb_lower = float(ma20 - 2 * sd20)
        bb_pct = (last - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else None
    else:
        bb_upper = bb_lower = bb_pct = None

    # ATR(14) for stop sizing
    if "High" in df.columns and "Low" in df.columns and len(df) >= 15:
        prev_close = close.shift(1)
        tr = (high - low).combine((high - prev_close).abs(), max).combine((low - prev_close).abs(), max)
        atr14 = float(tr.tail(14).mean())
        atr_pct = atr14 / last * 100 if last else None
    else:
        atr14 = atr_pct = None

    # Volume ratio vs 20-day avg
    if volume is not None and len(volume) >= 21:
        v_last = float(volume.iloc[-1])
        v_avg = float(volume.tail(20).mean())
        vol_ratio = v_last / v_avg if v_avg else None
    else:
        vol_ratio = None

    # 20-day breakout: today's close above the prior 20-day high
    breakout_20d = bool(high_20d is not None and last >= high_20d and len(close) >= 21
                        and last > float(close.iloc[-21:-1].max()))

    return {
        "last": last,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "sma50_series": _series_dump(sma50_series, tail=200),
        "sma200_series": _series_dump(sma200_series, tail=200),
        "above_sma50": (last > sma50) if sma50 else None,
        "above_sma200": (last > sma200) if sma200 else None,
        "stacked_uptrend": bool(sma20 and sma50 and sma200 and last > sma20 > sma50 > sma200),
        "stacked_downtrend": bool(sma20 and sma50 and sma200 and last < sma20 < sma50 < sma200),
        "golden_cross_recent": golden_cross_recent,
        "death_cross_recent": death_cross_recent,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pct_off_52w_high": pct_off_high,
        "rsi14": rsi_last,
        "macd_hist": float(macd_hist.iloc[-1]) if not macd_hist.empty else None,
        "macd_cross_up": macd_cross_up,
        "macd_cross_down": macd_cross_down,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_pct": bb_pct,
        "atr14": atr14,
        "atr_pct": atr_pct,
        "vol_ratio_20d": vol_ratio,
        "breakout_20d": breakout_20d,
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
