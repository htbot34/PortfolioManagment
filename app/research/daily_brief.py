"""Daily verdict - deterministic Python composition with a 3-signal gate.

Pipeline:
  1. Per-ticker analyst output (rule-based; LLM gated off by default).
  2. Scanner over the broader universe.
  3. Candidate selection (defense first, then offense).
  4. ``app.research.conviction.evaluate`` gates every candidate. A trade
     ONLY surfaces when technical + sector_momentum + news all pass.
  5. Watching list built from the top scanner candidates that didn't clear
     the gate.

Most days return ``no_trade`` because the 3-signal bar is intentionally
hard to clear.
"""
from datetime import date

from app.data import news as news_mod
from app.data import prices
from app.portfolio import store
from app.research import conviction, gate_telemetry, idea_funnel, sizing
from app.research.recid import make_rec_id


def _gate_entry(ticker: str, gate: dict | None = None,
                pre_block: str | None = None) -> dict:
    """Build one telemetry entry for ``gate_telemetry.record``.

    ``pre_block`` is set for candidates dropped before the conviction gate
    ran (regime suppression, soft veto); otherwise ``gate`` carries the
    ``conviction.evaluate`` result.
    """
    if pre_block:
        return {"ticker": ticker.upper(), "pre_block": pre_block}
    gate = gate or {}
    sigs = gate.get("signals") or {}
    trump_sig = sigs.get("trump") or {}
    reasons = gate.get("reasons") or {}
    # A "Trump promotion" is a qualification that would NOT have happened
    # under the old 3-of-3 rule: tech + trump + one of (sector, news) and
    # NOT both of sector+news passing. (When sector + news both passed,
    # Trump is just extra confluence, not a substitution.)
    sector_news_pass = bool(reasons.get("sector_momentum")) and bool(reasons.get("news"))
    trump_promoted = (bool(gate.get("qualifies"))
                       and bool(reasons.get("trump"))
                       and not sector_news_pass)
    return {
        "ticker": ticker.upper(),
        "pre_block": None,
        "qualifies": bool(gate.get("qualifies")),
        "promoted_by_insider": bool(gate.get("promoted_by_insider")),
        "earnings_block": gate.get("earnings_block"),
        "trump_block": gate.get("trump_block"),
        # Overlay blocks get their own telemetry buckets so they aren't
        # mis-attributed to a primary signal (notably trump).
        "correlation_block": gate.get("correlation_block"),
        "valuation_block": gate.get("valuation_block"),
        "reasons": reasons,
        "insider_score": int(gate.get("insider_score", 0) or 0),
        # Data-availability statuses (Phase A): keep "scored zero / no news"
        # distinct from "source unreachable" in the durable telemetry.
        "insider_status": gate.get("insider_status", "not_evaluated"),
        "news_status": gate.get("news_status", "unknown"),
        "trump_mention": bool(trump_sig.get("valence") in ("endorse", "attack")
                               or trump_sig.get("pass")),
        "trump_valence": trump_sig.get("valence", "none"),
        "trump_as_of": trump_sig.get("as_of"),
        "trump_source": trump_sig.get("source"),
        "trump_promoted": trump_promoted,
    }


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
                       scan_result: dict, exclude: set[str],
                       macro: dict | None = None,
                       account: store.Account | None = None,
                       soft_veto: set[str] | None = None,
                       gate_log: list | None = None) -> dict | None:
    """Return a defense verdict ONLY if a held name has conviction-5 sell or
    trim AND clears the 3-signal conviction gate (technical + bearish news +
    sector weakness). This is intentional: per spec, exits are thesis-driven,
    not mechanical. A position being over the 25% cap is no longer enough on
    its own to trigger an automatic trim.
    """
    veto = soft_veto or set()
    log = gate_log if gate_log is not None else []
    for r in recommendations:
        if r.get("conviction", 0) != 5:
            continue
        if r.get("action") not in ("sell", "trim"):
            continue
        ticker = (r.get("ticker") or "").upper()
        if ticker in veto:
            # soft veto: user has rejected this ticker repeatedly
            log.append(_gate_entry(ticker, pre_block="soft_veto"))
            continue
        gate = conviction.evaluate(r, direction="short", macro=macro or {},
                                    news_fetcher=news_mod.company_news_with_status,
                                    action=r.get("action"))
        log.append(_gate_entry(ticker, gate))
        if not gate["qualifies"]:
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
        weight_pct = (r.get('position') or {}).get('weight_pct')
        if r["action"] == "trim":
            short = f"position weight {weight_pct:.0f}%" if weight_pct else "overweight"
            invalidation = f"Weight back under 25% (after trim) - then hold the remaining core."
        else:
            short = "confirmed downtrend"
            invalidation = f"Daily close back above SMA50 (${(t.get('sma50') or 0):.2f}) on volume - reconsider exit."
        # severity for trim sizing: a stacked downtrend + conviction-5
        # qualifies as severely damaged -> trim 50% instead of the default 30.
        severity = "severe" if t.get("stacked_downtrend") else "normal"
        pos = (account.position(r["ticker"]) if account else None)
        size = sizing.compute_size(r["action"], r["ticker"], price, pos,
                                    account or store.load(), gate=None,
                                    severity=severity)
        today = date.today().isoformat()
        rec_id = make_rec_id(today, r["ticker"], r["action"])
        return {
            "verdict": "defense",
            "headline": f"{action_word} {r['ticker']} - {short}.",
            "primary_action": {
                "rec_id": rec_id,
                "ticker": r["ticker"],
                "action": r["action"],
                "entry": f"~${price:.2f}" if price else "market",
                "stop": stop_txt,
                "target": target_txt,
                "size_pct": size.get("target_weight_pct"),
                "size": size,
                "shares": size.get("shares"),
                "dollars": size.get("dollars"),
                "unrealized_pnl_on_action": size.get("unrealized_pnl_on_action"),
                "thesis": thesis,
                "invalidation": invalidation,
                "conviction": 5,
                "evidence": _evidence_for_defense(r, t, weight_pct),
                "conviction_gate": gate,
            },
            "secondary_actions": [],
            "market_snapshot": macro_line,
            "watching": _build_watching(scan_result, exclude),
            "generated_for": today,
        }
    return None


# Buckets the gate considers for long entries. Order matters: the first
# scanner row of each ticker found across these buckets is the payload used
# for the conviction gate, so list highest-quality setup types first.
_BULLISH_BUCKETS = (
    "breakouts",
    "momentum_continuation",
    "new_52w_highs",
    "macd_bullish_cross",
    "pullbacks_to_support",
    "oversold_bounces",
)

_SETUP_LABELS = {
    "breakouts": "20-day breakout on volume",
    "momentum_continuation": "Momentum continuation in an established uptrend",
    "new_52w_highs": "Breaking to new 52-week highs",
    "macd_bullish_cross": "Bullish MACD cross with momentum",
    "pullbacks_to_support": "Pullback to support in uptrend",
    "oversold_bounces": "Deep oversold bounce with MACD turn",
}


def _trade_from_scanner(scan_result: dict, macro: dict, macro_line: str,
                         exclude: set[str],
                         account: store.Account | None = None,
                         soft_veto: set[str] | None = None,
                         regime: dict | None = None,
                         gate_log: list | None = None,
                         funnel: dict | None = None) -> dict | None:
    """Promote a setup to a trade verdict via the conviction gate.

    Candidates are drawn from every bullish scanner bucket (breakouts,
    momentum continuation, new 52-week highs, MACD cross-ups, pullbacks,
    oversold bounces) and ordered by the idea funnel's confluence score
    (multi-source confirmation) when available, then by bucket priority.

    A **light pre-filter** (volume above average + MACD histogram positive)
    blocks only obvious noise so the conviction gate is not flooded with
    LLM-classified news calls. The binding decision is still the 3-signal
    conviction gate (technical + sector_momentum + news), with the same
    insider 2-of-3 promotion path as before.

    Regime gating:
      - breakdown: no new buys (defense only).
      - chop:      insider 2-of-3 promotion path disabled.
    """
    if _macro_risk_off(macro):
        return None
    regime_name = (regime or {}).get("regime")
    breakdown = regime_name == "breakdown"
    allow_promo = regime_name != "chop"
    veto = soft_veto or set()
    log = gate_log if gate_log is not None else []

    # 1. Collect the first scanner row for each ticker across bullish buckets.
    setups: dict[str, tuple[str, dict]] = {}
    for bucket in _BULLISH_BUCKETS:
        for s in scan_result.get("buckets", {}).get(bucket, []) or []:
            t = (s.get("ticker") or "").upper()
            if t and t not in setups:
                setups[t] = (bucket, s)

    # 2. Order: funnel-confluence tickers first (highest points first), then
    #    any remaining setups in canonical (bucket, position) order.
    ordered: list[tuple[str, str, dict]] = []
    used: set[str] = set()
    for idea in (funnel or {}).get("confluence") or []:
        t = (idea.get("ticker") or "").upper()
        if t in setups and t not in used:
            bucket, s = setups[t]
            ordered.append((t, bucket, s))
            used.add(t)
    for bucket in _BULLISH_BUCKETS:
        for s in scan_result.get("buckets", {}).get(bucket, []) or []:
            t = (s.get("ticker") or "").upper()
            if t and t not in used and t in setups:
                ordered.append((t, bucket, s))
                used.add(t)

    # 3. Evaluate each candidate through the conviction gate.
    for ticker, bucket, s in ordered:
        if s.get("held") or ticker in exclude:
            continue
        if ticker in veto:
            log.append(_gate_entry(ticker, pre_block="soft_veto"))
            continue
        vol = s.get("vol_ratio_20d") or 0
        macd_h = s.get("macd_hist") or 0
        if not (vol >= 1.2 and macd_h > 0):
            continue
        if breakdown:
            log.append(_gate_entry(ticker, pre_block="regime"))
            continue
        gate = conviction.evaluate(s, direction="long", macro=macro,
                                    news_fetcher=news_mod.company_news_with_status,
                                    action="new_buy", portfolio=account,
                                    allow_insider_promotion=allow_promo)
        log.append(_gate_entry(ticker, gate))
        if not gate["qualifies"]:
            continue
        label = _SETUP_LABELS.get(bucket, "Setup cleared the conviction gate")
        return _build_trade(s, "buy", label, macro_line, scan_result,
                             exclude, gate=gate, account=account,
                             regime_name=regime_name)
    return None


def _evidence_for_entry(s: dict, t: dict) -> list[dict]:
    """Citations the user can check: price level, technical signals, theme."""
    out: list[dict] = []
    if s.get("rsi14") is not None:
        rsi = s["rsi14"]
        if rsi <= 30:
            supports = "deep oversold (mean-reversion setup)"
        elif rsi >= 70:
            supports = "extended (avoid chasing)"
        else:
            supports = "momentum in the sweet spot"
        out.append({"source": "technical", "ref": f"RSI {rsi:.0f}", "supports": supports})
    if s.get("macd_hist") is not None:
        macd = s["macd_hist"]
        out.append({
            "source": "technical",
            "ref": f"MACD histogram {macd:+.2f}",
            "supports": "bullish" if macd > 0 else "bearish",
        })
    if s.get("vol_ratio_20d"):
        out.append({
            "source": "technical",
            "ref": f"volume {s['vol_ratio_20d']:.1f}x 20d avg",
            "supports": "confirmation" if s["vol_ratio_20d"] >= 1.5 else "weak confirmation",
        })
    if t.get("golden_cross_recent"):
        out.append({"source": "technical", "ref": "golden cross within 20 days",
                    "supports": "long-term trend turning up"})
    if t.get("death_cross_recent"):
        out.append({"source": "technical", "ref": "death cross within 20 days",
                    "supports": "long-term trend rolling over"})
    if s.get("pct_off_52w_high") is not None:
        out.append({
            "source": "technical",
            "ref": f"{s['pct_off_52w_high']:.0f}% off 52-week high",
            "supports": "drawdown context",
        })
    if s.get("theme"):
        out.append({"source": "theme", "ref": s["theme"], "supports": "fits investor mandate"})
    return out


def _evidence_for_defense(r: dict, t: dict, weight_pct: float | None) -> list[dict]:
    """Citations for a defensive call (trim/exit on a held position)."""
    out: list[dict] = []
    if weight_pct is not None:
        out.append({
            "source": "constraint",
            "ref": f"weight {weight_pct:.0f}%",
            "supports": "exceeds 25% single-position cap" if weight_pct > 25 else "approaching cap",
        })
    if t.get("stacked_downtrend"):
        out.append({"source": "technical", "ref": "price < SMA20 < SMA50 < SMA200",
                    "supports": "confirmed downtrend"})
    if t.get("rsi14") is not None and t["rsi14"] <= 30:
        out.append({"source": "technical", "ref": f"RSI {t['rsi14']:.0f}",
                    "supports": "oversold (allow bounce before trimming)"})
    if t.get("death_cross_recent"):
        out.append({"source": "technical", "ref": "death cross within 20 days",
                    "supports": "long-term trend rolling over"})
    if t.get("pct_off_52w_high") is not None and t["pct_off_52w_high"] < -25:
        out.append({"source": "technical", "ref": f"{t['pct_off_52w_high']:.0f}% off 52-week high",
                    "supports": "structural drawdown"})
    return out


def _build_trade(s: dict, action: str, reason: str, macro_line: str,
                 scan_result: dict, exclude: set[str],
                 gate: dict | None = None,
                 account: store.Account | None = None,
                 regime_name: str | None = None) -> dict:
    price = s.get("price")
    # Pull a fuller technicals snapshot for the stop/target sizing
    t = prices.technicals(s["ticker"])
    atr = t.get("atr14") or (price * 0.04 if price else None)
    risk_amount = (atr * 1.5) if atr else (price * 0.05 if price else 0)
    stop_px = price - risk_amount if price else None
    target_px = price + risk_amount * 3 if price else None  # 3:1 R:R
    thesis = (
        f"{s['ticker']} - {reason}. RSI {(s.get('rsi14') or 0):.0f}, "
        f"volume {(s.get('vol_ratio_20d') or 1):.1f}x 20-day average. "
        f"Theme: {s.get('theme') or 'growth'}. Long-term hold target."
    )
    # Sizing: if the ticker is already held, treat as ADD; else NEW_BUY.
    acct = account or store.load()
    pos = acct.position(s["ticker"]) if acct else None
    sized_action = "add" if pos else "new_buy"
    size = sizing.compute_size(sized_action, s["ticker"], price, pos, acct,
                                gate=gate, regime=regime_name)
    # Publicly the action stays "buy" so the UI logic doesn't fork - the
    # nuance (add vs new) is captured in the size.display string.
    today = date.today().isoformat()
    rec_id = make_rec_id(today, s["ticker"], action)
    return {
        "verdict": "trade",
        "headline": f"{action.upper()} {s['ticker']} - {reason}.",
        "primary_action": {
            "rec_id": rec_id,
            "ticker": s["ticker"],
            "action": action,
            "entry": f"~${price:.2f}" if price else "",
            "stop": f"${stop_px:.2f}" if stop_px else "",
            "target": f"${target_px:.2f}" if target_px else "",
            "size_pct": size.get("target_weight_pct") or 5,
            "size": size,
            "shares": size.get("shares"),
            "dollars": size.get("dollars"),
            "thesis": thesis,
            "invalidation": f"Daily close below ${stop_px:.2f}" if stop_px else "structural break",
            "conviction": 5,
            "evidence": _evidence_for_entry(s, t),
            "conviction_gate": gate,
        },
        "secondary_actions": [],
        "market_snapshot": macro_line,
        "watching": _build_watching(scan_result, exclude | {s["ticker"].upper()}),
        "generated_for": today,
    }


def _regime_banner(regime: dict | None) -> str | None:
    """A one-line banner string for the gated regimes, else None."""
    name = (regime or {}).get("regime")
    if name == "breakdown":
        return "Breakdown regime - defense only. New buys are suppressed."
    if name == "chop":
        return "Chop regime - gate tightened, insider promotion disabled."
    return None


def build(macro: dict, recommendations: list[dict], review: dict,
          candidates: dict, exposures: dict, scan_result: dict,
          headlines: list[dict] | None = None,
          account: store.Account | None = None,
          user_preferences: dict | None = None,
          regime: dict | None = None,
          funnel: dict | None = None) -> dict:
    """Return today's verdict. Deterministic - no LLM call.

    ``account`` is optional - when provided, recommendations get sized
    against current cash + positions via ``app.research.sizing``. When None,
    sizing falls back to a freshly loaded account.

    ``user_preferences`` is the output of
    ``app.research.learning.derive_user_preferences`` over the last ~30 days
    of rec_history.yaml. Tickers under soft veto (>=2 rejections by the
    user) are demoted to the watching list instead of becoming a primary
    action.

    ``regime`` is the ``app.research.regime.detect_regime`` output. It gates
    behavior: breakdown suppresses all new buys (defense only); chop disables
    the insider 2-of-3 promotion; risk_off downgrades new-buy sizing.
    """
    macro_line = _macro_line(macro)
    held = {p["ticker"].upper() for p in (exposures.get("positions") or [])}
    if account is None:
        account = store.load()

    from app.research.learning import soft_veto_tickers  # local to avoid cycle
    soft_veto = soft_veto_tickers(user_preferences or {})
    banner = _regime_banner(regime)

    def _stamp(verdict: dict) -> dict:
        if banner:
            verdict["regime_banner"] = banner
        if regime:
            verdict["regime"] = regime
        return verdict

    gate_log: list = []

    if _macro_risk_off(macro):
        verdict = _stamp(_no_trade(
            "Macro is risk-off (high VIX or broken trend). Wait.",
            macro_line, _build_watching(scan_result, held)))
    else:
        defense = _defense_from_book(recommendations, macro_line, scan_result,
                                      held, macro=macro, account=account,
                                      soft_veto=soft_veto, gate_log=gate_log)
        if defense:
            verdict = _stamp(defense)
        else:
            trade = _trade_from_scanner(scan_result, macro, macro_line, held,
                                          account=account, soft_veto=soft_veto,
                                          regime=regime, gate_log=gate_log,
                                          funnel=funnel)
            if trade:
                verdict = _stamp(trade)
            else:
                verdict = _stamp(_no_trade(
                    "No setup meets the conviction bar.",
                    macro_line, _build_watching(scan_result, held)))

    # Gate telemetry: record why candidates did / did not clear, so a
    # no-trade verdict is explainable. The brief carries today's record and
    # the 30-day rollup; build_site.py persists the record to disk.
    today_str = date.today().isoformat()
    telem = gate_telemetry.record(
        [{"ticker": e.get("ticker", "")} for e in gate_log], gate_log, today_str)
    verdict["telemetry"] = telem
    verdict["telemetry_30d"] = gate_telemetry.rollup(
        gate_telemetry.merged_history(telem))

    # On a no-trade day, surface the funnel's highest-confluence ideas. These
    # are NOT recommendations - they did not clear the conviction gate - but
    # they tell the user what is worth watching.
    if verdict.get("verdict") == "no_trade" and funnel is not None:
        verdict["high_confluence"] = idea_funnel.top_independent_confluence(
            funnel, n=3)

    return verdict
