"""Daily verdict - deterministic Python composition.

The brief is no longer produced by a separate LLM call. The synthesis call
kept tripping Azure's jailbreak classifier and rate limits. Instead we:

  1. Read each held position's per-ticker LLM analysis (which already passes
     content filter at conviction-rated granularity).
  2. Read the scanner buckets for high-quality setups in the broader
     universe (textbook breakouts, oversold quality bounces).
  3. Apply a strict deterministic gate to decide verdict:
       defense   if any held name has conviction-5 sell or trim
       trade     if scanner has a true breakout (vol>=1.8x AND RSI 55-68)
                 OR an oversold bounce on a quality name (RSI<=30 near SMA200)
       no_trade  otherwise (the default)
  4. Build watching list from the top 3-5 scanner candidates that didn't
     meet the trade bar.

This is intentionally conservative. Most days return no_trade.
"""
from datetime import date

from app.data import prices


def _no_trade(reason: str, macro_line: str, watching: list[str]) -> dict:
    return {
        "verdict": "no_trade",
        "headline": f"No trade today. {reason}",
        "primary_action": None,
        "secondary_actions": [],
        "market_snapshot": macro_line,
        "watching": watching,
        "generated_for": date.today().isoformat(),
    }


def _macro_line(macro: dict) -> str:
    idx = macro.get("indices") or {}
    spx = idx.get("SPX", {})
    vix = idx.get("VIX", {})
    leaders = (macro.get("leaders") or [])[:1]
    parts = []
    if spx.get("day_change_pct") is not None:
        parts.append(f"SPX {spx['day_change_pct']:+.2f}%")
    if vix.get("price") is not None:
        parts.append(f"VIX {vix['price']:.1f}")
    if leaders:
        l = leaders[0]
        parts.append(f"{l['name']} leads {l['day_change_pct']:+.1f}%")
    return ", ".join(parts) + "." if parts else ""


def _macro_risk_off(macro: dict) -> bool:
    vix = (macro.get("indices") or {}).get("VIX", {}).get("price")
    if vix and vix > 22:
        return True
    spx = (macro.get("indices") or {}).get("SPX", {})
    pct = spx.get("pct_off_52w_high")
    return bool(pct is not None and pct < -10)


def _build_watching(scan_result: dict, exclude: set[str]) -> list[str]:
    watching = []
    seen: set[str] = set()
    bucket_labels = {
        "breakouts": "watching for clean breakout",
        "momentum_continuation": "watching for pullback entry",
        "oversold_bounces": "watching for reversal confirmation",
        "pullbacks_to_support": "watching SMA50 hold",
        "new_52w_highs": "fresh 52w high, waiting for retest",
    }
    for bucket, label in bucket_labels.items():
        for s in scan_result.get("buckets", {}).get(bucket, [])[:2]:
            t = s["ticker"].upper()
            if t in seen or t in exclude:
                continue
            seen.add(t)
            watching.append(f"{t} - {label}")
            if len(watching) >= 5:
                return watching
    return watching


def _defense_from_book(recommendations: list[dict], macro_line: str,
                       scan_result: dict, exclude: set[str]) -> dict | None:
    """Return defense verdict if any held name hit conviction-5 sell or trim."""
    for r in recommendations:
        if r.get("conviction", 0) != 5:
            continue
        if r.get("action") not in ("sell", "trim"):
            continue
        q = r.get("quote") or {}
        t = r.get("technicals") or {}
        sma200 = t.get("sma200")
        atr = t.get("atr14")
        price = q.get("price")
        # For a trim/sell, "stop" = where we'd reverse and stay,
        # "target" = the structural exit level.
        if r["action"] == "trim":
            stop_txt = f"${price + (atr or price * 0.05):.2f} (give it room)" if price else "n/a"
            target_txt = f"trim 25-50% at ~${price:.2f}" if price else "trim 25-50%"
        else:
            stop_txt = "n/a (closing position)"
            target_txt = f"exit fully; reload only above ${sma200:.2f}" if sma200 else "exit fully"
        thesis = r.get("thesis") or ""
        action_word = "Trim" if r["action"] == "trim" else "Exit"
        if r["action"] == "trim":
            short = f"position weight {(r.get('position') or {}).get('weight_pct', 0):.0f}%" if (r.get('position') or {}).get('weight_pct') else "overweight"
        else:
            short = "confirmed downtrend"
        return {
            "verdict": "defense",
            "headline": f"{action_word} {r['ticker']} - {short}.",
            "primary_action": {
                "ticker": r["ticker"],
                "action": r["action"],
                "entry": f"~${price:.2f}" if price else "market",
                "stop": stop_txt,
                "target": target_txt,
                "size_pct": None,
                "thesis": thesis,
                "invalidation": (r.get("key_risks") or [""])[0],
                "conviction": 5,
            },
            "secondary_actions": [],
            "market_snapshot": macro_line,
            "watching": _build_watching(scan_result, exclude),
            "generated_for": date.today().isoformat(),
        }
    return None


_QUALITY_THEMES = {
    "Mega cap tech", "Semiconductors", "AI infra / data",
    "Cloud / SaaS", "Cybersecurity", "Quality compounders",
    "SMR / nuclear / clean energy",
}


def _trade_from_scanner(scan_result: dict, macro: dict, macro_line: str,
                         exclude: set[str]) -> dict | None:
    """Promote a high-quality scanner setup to a trade verdict.

    Bars (intentionally strict so most days have no trade):

      Breakout requires ALL of:
        - 20-day breakout AND new 52w high (or within 1% of it)
        - vol_ratio_20d >= 2.0
        - RSI in [55, 65] (momentum without being extended)
        - MACD histogram positive
        - Quality theme

      Oversold quality bounce requires ALL of:
        - RSI <= 25
        - MACD bullish cross today (positive divergence)
        - Near SMA200 (within 6%)
        - vol_ratio_20d >= 1.2
        - Quality theme

    Skip if macro is risk-off.
    """
    if _macro_risk_off(macro):
        return None
    for s in scan_result["buckets"].get("breakouts", []):
        if s.get("held") or s["ticker"].upper() in exclude:
            continue
        rsi = s.get("rsi14") or 0
        vol = s.get("vol_ratio_20d") or 0
        macd_h = s.get("macd_hist") or 0
        off52 = s.get("pct_off_52w_high") or -100
        theme = s.get("theme") or ""
        if not (55 <= rsi <= 65 and vol >= 2.0 and macd_h > 0
                and off52 >= -2 and theme in _QUALITY_THEMES):
            continue
        return _build_trade(s, "buy", "Quality breakout to new highs on heavy volume",
                             macro_line, scan_result, exclude)
    for s in scan_result["buckets"].get("oversold_bounces", []):
        if s.get("held") or s["ticker"].upper() in exclude:
            continue
        rsi = s.get("rsi14") or 100
        macd_h = s.get("macd_hist") or -1
        vol = s.get("vol_ratio_20d") or 0
        theme = s.get("theme") or ""
        if not (rsi <= 25 and macd_h > 0 and vol >= 1.2
                and theme in _QUALITY_THEMES):
            continue
        return _build_trade(s, "buy", "Deep oversold quality name with bullish MACD cross",
                             macro_line, scan_result, exclude)
    return None


def _build_trade(s: dict, action: str, reason: str, macro_line: str,
                 scan_result: dict, exclude: set[str]) -> dict:
    price = s.get("price")
    # Pull a fuller technicals snapshot for the stop/target sizing
    t = prices.technicals(s["ticker"])
    atr = t.get("atr14") or (price * 0.04 if price else None)
    sma50 = t.get("sma50")
    risk_amount = (atr * 1.5) if atr else (price * 0.05 if price else 0)
    stop_px = price - risk_amount if price else None
    target_px = price + risk_amount * 3 if price else None  # 3:1 R:R
    thesis = (
        f"{s['ticker']} - {reason}. RSI {(s.get('rsi14') or 0):.0f}, "
        f"volume {(s.get('vol_ratio_20d') or 1):.1f}x 20-day average. "
        f"Theme: {s.get('theme') or 'growth'}. Long-term hold target."
    )
    return {
        "verdict": "trade",
        "headline": f"{action.upper()} {s['ticker']} - {reason}.",
        "primary_action": {
            "ticker": s["ticker"],
            "action": action,
            "entry": f"~${price:.2f}" if price else "",
            "stop": f"${stop_px:.2f}" if stop_px else "",
            "target": f"${target_px:.2f}" if target_px else "",
            "size_pct": 5,
            "thesis": thesis,
            "invalidation": f"Daily close below ${stop_px:.2f}" if stop_px else "structural break",
            "conviction": 5,
        },
        "secondary_actions": [],
        "market_snapshot": macro_line,
        "watching": _build_watching(scan_result, exclude | {s["ticker"].upper()}),
        "generated_for": date.today().isoformat(),
    }


def build(macro: dict, recommendations: list[dict], review: dict,
          candidates: dict, exposures: dict, scan_result: dict,
          headlines: list[dict] | None = None) -> dict:
    """Return today's verdict. Deterministic - no LLM call."""
    macro_line = _macro_line(macro)
    held = {p["ticker"].upper() for p in (exposures.get("positions") or [])}

    if _macro_risk_off(macro):
        return _no_trade("Macro is risk-off (high VIX or broken trend). Wait.",
                          macro_line, _build_watching(scan_result, held))

    defense = _defense_from_book(recommendations, macro_line, scan_result, held)
    if defense:
        return defense

    trade = _trade_from_scanner(scan_result, macro, macro_line, held)
    if trade:
        return trade

    return _no_trade("No setup meets the conviction bar.",
                      macro_line, _build_watching(scan_result, held))
