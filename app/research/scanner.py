"""Daily opportunity scanner.

Runs across the curated universe every morning and categorizes setups:
  - breakouts:           20-day price breakout with volume confirmation
  - momentum_continuation: clean uptrend, RSI in the sweet spot (45-65)
  - oversold_bounces:    RSI <= 32 with price near SMA200 support
  - pullbacks_to_support: uptrend intact, RSI 38-50, price near SMA50
  - macd_bullish_cross:  MACD histogram just flipped positive
  - macd_bearish_cross:  MACD histogram just flipped negative
  - new_52w_highs:       fresh 52-week high
  - rsi_extreme_overbought: RSI >= 75 (warning / trim candidate)

The scanner is purely mechanical. The LLM picks the best subset and writes
the trade plan.
"""
from app.data import prices
from app.research import universe


def _enrich(ticker: str) -> dict | None:
    # fast=True skips the yfinance.info fundamentals call - the scanner only
    # needs price + technicals + theme (theme comes from the universe map).
    # sector / pe_ratio / market_cap are not used downstream from the scan.
    q = prices.quote(ticker, fast=True)
    t = prices.technicals(ticker)
    if not q.price or t.get("error"):
        return None
    return {
        "ticker": ticker,
        "theme": universe.theme_of(ticker),
        "price": q.price,
        "day_change_pct": q.day_change_pct,
        "sector": q.sector,
        "market_cap": q.market_cap,
        "pe_ratio": q.pe_ratio,
        "sma20": t.get("sma20"),
        "sma50": t.get("sma50"),
        "sma200": t.get("sma200"),
        "above_sma50": t.get("above_sma50"),
        "above_sma200": t.get("above_sma200"),
        "rsi14": t.get("rsi14"),
        "macd_hist": t.get("macd_hist"),
        "macd_cross_up": t.get("macd_cross_up"),
        "macd_cross_down": t.get("macd_cross_down"),
        "bb_pct": t.get("bb_pct"),
        "bb_upper": t.get("bb_upper"),
        "bb_lower": t.get("bb_lower"),
        "atr14": t.get("atr14"),
        "atr_pct": t.get("atr_pct"),
        "vol_ratio_20d": t.get("vol_ratio_20d"),
        "high_52w": t.get("high_52w"),
        "pct_off_52w_high": t.get("pct_off_52w_high"),
        "stacked_uptrend": t.get("stacked_uptrend"),
        "stacked_downtrend": t.get("stacked_downtrend"),
        "breakout_20d": t.get("breakout_20d"),
    }


def _is_breakout(r: dict) -> bool:
    return bool(
        r["breakout_20d"]
        and r.get("vol_ratio_20d") and r["vol_ratio_20d"] >= 1.3
        and r.get("rsi14") and r["rsi14"] < 80
    )


def _is_momentum(r: dict) -> bool:
    return bool(
        r["stacked_uptrend"]
        and r.get("rsi14") and 45 <= r["rsi14"] <= 65
        and r.get("pct_off_52w_high") and r["pct_off_52w_high"] >= -15
    )


def _is_oversold_bounce(r: dict) -> bool:
    if not (r.get("rsi14") and r.get("price") and r.get("sma200")):
        return False
    near_sma200 = abs(r["price"] / r["sma200"] - 1) < 0.08
    return r["rsi14"] <= 32 and near_sma200


def _is_pullback(r: dict) -> bool:
    if not (r.get("rsi14") and r.get("price") and r.get("sma50") and r.get("sma200")):
        return False
    return (
        r["price"] > r["sma200"]
        and 38 <= r["rsi14"] <= 50
        and abs(r["price"] / r["sma50"] - 1) < 0.05
    )


def _is_new_52w_high(r: dict) -> bool:
    return bool(r.get("price") and r.get("high_52w") and r["price"] >= r["high_52w"] * 0.995)


def _is_overbought(r: dict) -> bool:
    return bool(r.get("rsi14") and r["rsi14"] >= 75)


def _setup_score(r: dict, kind: str) -> float:
    """Heuristic ranking so the LLM gets the strongest examples in each bucket."""
    score = 0.0
    if kind == "breakout":
        score = (r.get("vol_ratio_20d") or 1.0) + (1 - abs(50 - (r.get("rsi14") or 50)) / 50)
    elif kind == "momentum":
        score = 1 + (1 - abs(55 - (r.get("rsi14") or 55)) / 50)
        if r.get("macd_hist") and r["macd_hist"] > 0:
            score += 0.5
    elif kind == "oversold_bounce":
        score = 2 - (r.get("rsi14") or 30) / 30
        if r.get("macd_cross_up"):
            score += 1
    elif kind == "pullback":
        score = 1
        if r.get("macd_hist") and r["macd_hist"] > 0:
            score += 0.5
    elif kind == "new_52w_high":
        score = 1 + (r.get("vol_ratio_20d") or 1.0) * 0.5
    return float(score)


def scan(held: set[str] | None = None) -> dict:
    # Warm the price cache for the whole universe in parallel up front so the
    # per-ticker _enrich loop below hits the in-memory cache instead of making
    # one network round-trip at a time. Purely a fetch-ordering optimisation -
    # the loop's logic, bucketing, and fallbacks are unchanged.
    prices.prefetch(universe.all_tickers())
    held = {t.upper() for t in (held or set())}
    rows: list[dict] = []
    for ticker in universe.all_tickers():
        info = _enrich(ticker)
        if info:
            info["held"] = ticker.upper() in held
            rows.append(info)

    buckets: dict[str, list[dict]] = {
        "breakouts": [],
        "momentum_continuation": [],
        "oversold_bounces": [],
        "pullbacks_to_support": [],
        "macd_bullish_cross": [],
        "macd_bearish_cross": [],
        "new_52w_highs": [],
        "rsi_extreme_overbought": [],
    }

    for r in rows:
        if _is_breakout(r):
            buckets["breakouts"].append({**r, "score": _setup_score(r, "breakout")})
        if _is_momentum(r):
            buckets["momentum_continuation"].append({**r, "score": _setup_score(r, "momentum")})
        if _is_oversold_bounce(r):
            buckets["oversold_bounces"].append({**r, "score": _setup_score(r, "oversold_bounce")})
        if _is_pullback(r):
            buckets["pullbacks_to_support"].append({**r, "score": _setup_score(r, "pullback")})
        if r.get("macd_cross_up"):
            buckets["macd_bullish_cross"].append(r)
        if r.get("macd_cross_down"):
            buckets["macd_bearish_cross"].append(r)
        if _is_new_52w_high(r):
            buckets["new_52w_highs"].append({**r, "score": _setup_score(r, "new_52w_high")})
        if _is_overbought(r):
            buckets["rsi_extreme_overbought"].append(r)

    for k in buckets:
        if buckets[k] and "score" in buckets[k][0]:
            buckets[k].sort(key=lambda x: x.get("score", 0), reverse=True)
        buckets[k] = buckets[k][:8]

    return {
        "universe_size": len(rows),
        "buckets": buckets,
        "top_movers_up": sorted(
            (r for r in rows if r.get("day_change_pct") is not None),
            key=lambda x: x["day_change_pct"], reverse=True,
        )[:10],
        "top_movers_down": sorted(
            (r for r in rows if r.get("day_change_pct") is not None),
            key=lambda x: x["day_change_pct"],
        )[:10],
    }
