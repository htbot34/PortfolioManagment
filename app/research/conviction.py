"""Conviction gate orchestrator.

``evaluate()`` runs the technical signal first (a hard prerequisite - it can
never be substituted), then sector_momentum and news. A recommendation
``qualifies`` via one of two paths:

  PRIMARY PATH       all three primary signals (technical, sector, news) pass.
  INSIDER PROMOTION  exactly two of the three pass, technical is one of them,
                     and the insider cluster score for the direction is >= 2.
                     The failing signal must be sector or news, never technical.

Phase 1 added: semantic LLM news classification and the earnings-window
hard block. Phase 2 added: the insider-promotion path.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from app.config import risk_profile
from app.research import insider_signal, news_classifier, signals, trump_signal


def _extract_sector(payload: dict) -> str:
    """Find the sector string on either a scanner row or analyst payload.

    Scanner rows skip yfinance fundamentals (sector is None) but carry a
    ``theme`` we can translate to a SPDR sector via THEME_TO_SECTOR.
    """
    if not isinstance(payload, dict):
        return ""
    sector = (
        payload.get("sector")
        or (payload.get("quote") or {}).get("sector")
        or ""
    )
    if sector:
        return sector
    theme = payload.get("theme")
    if theme:
        return signals.THEME_TO_SECTOR.get(theme, "")
    return ""


def _trading_days_until(target: date) -> int:
    """Count Mon-Fri days from today (exclusive) to ``target`` (inclusive).

    Holidays are ignored - this is a deterministic, free approximation and a
    holiday only ever makes the count slightly generous, which is the safe
    direction for a "wait for earnings" block.
    """
    today = date.today()
    if target <= today:
        return 0
    days = 0
    d = today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _insider_signal(ticker: str, direction: str, payload: dict,
                    fetcher: Callable[[str], list[dict]] | None) -> dict:
    """Compute the insider cluster signal for ``direction``.

    Transactions come from ``payload['insider_transactions']`` if present,
    else from ``fetcher`` if given, else from ``insider.recent_form4_transactions``.

    A fetch failure (CIK 403, EDGAR outage, ...) is propagated as
    ``data_available=False`` on the result so the 2-of-3 promotion path
    cannot silently fail-as-zero — it can fail loudly instead.
    """
    txns = payload.get("insider_transactions")
    fetch_error: str | None = None
    fetch_failed = False
    if txns is None:
        if fetcher is not None:
            try:
                txns = fetcher(ticker)
            except Exception as e:
                txns = []
                fetch_failed = True
                fetch_error = f"{type(e).__name__}: {e}"
        else:
            try:
                from app.data import insider
                txns = insider.recent_form4_transactions(ticker)
                err = insider.LAST_FETCH_ERRORS.get(ticker.upper())
                if err:
                    fetch_failed = True
                    fetch_error = err
            except Exception as e:
                txns = []
                fetch_failed = True
                fetch_error = f"{type(e).__name__}: {e}"
    txns = txns or []
    kwargs = {"data_available": not fetch_failed, "error": fetch_error}
    if direction == "long":
        sc = insider_signal.insider_cluster_score(ticker, txns, **kwargs)
    else:
        sc = insider_signal.insider_cluster_score_short(ticker, txns, **kwargs)
    # An unavailable signal cannot pass -- the gate must NOT promote on
    # data we don't have.
    sc["pass"] = sc.get("score", 0) >= 2 and sc.get("data_available", True)
    return sc


def _gate_config() -> dict:
    """Read the gate knobs from risk_profile.yaml, falling back to defaults
    so the gate stays usable on a partially-populated config."""
    try:
        cfg = (risk_profile() or {}).get("gate") or {}
    except Exception:
        cfg = {}
    return {
        "trump_signal_enabled": bool(cfg.get("trump_signal_enabled", True)),
        "trump_ttl_days": int(cfg.get("trump_ttl_days", 30)),
        "trump_min_confidence": float(cfg.get("trump_min_confidence", 0.6)),
        "trump_confluence_min": int(cfg.get("trump_confluence_min", 2)),
        "trump_solo_with_technical": bool(cfg.get("trump_solo_with_technical", False)),
        "trump_attack_vetoes_longs": bool(cfg.get("trump_attack_vetoes_longs", True)),
    }


def evaluate(
    ticker_payload: dict,
    direction: str,
    macro: dict,
    news_fetcher: Callable[[str], list[dict]] | None = None,
    action: str | None = None,
    insider_fetcher: Callable[[str], list[dict]] | None = None,
    portfolio=None,
    prices_provider: Callable | None = None,
    allow_insider_promotion: bool = True,
    fundamentals: dict | None = None,
    sector_comparables: list[dict] | None = None,
    gate_config: dict | None = None,
) -> dict:
    """Run the conviction gate against ``ticker_payload`` for ``direction``.

    Returns::

        {
          "qualifies": bool,
          "signals": {"technical": ..., "sector_momentum": ..., "news": ...,
                       "insider": ...},   # insider only present if evaluated
          "summary": "technical=PASS sector_momentum=PASS news=fail",
          "promoted_by_insider": bool,
          "earnings_block": "<reason>"   # only when the earnings block fired
        }

    Technical is evaluated first and short-circuits the whole gate on failure
    (it can never be substituted). When technical passes, both sector and news
    are always evaluated so the 2-of-3 promotion path can be assessed.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    out_signals: dict[str, dict] = {}

    tech = signals.technical_signal(ticker_payload, direction)
    out_signals["technical"] = tech
    if not tech["pass"]:
        # Technical is a hard prerequisite - no path qualifies without it.
        result = _result(False, out_signals)
        result["promoted_by_insider"] = False
        return result

    sector = _extract_sector(ticker_payload)
    sec = signals.sector_momentum_signal(sector, macro, direction)
    out_signals["sector_momentum"] = sec

    news_items = ticker_payload.get("news")
    if news_items is None and news_fetcher is not None:
        tk = ticker_payload.get("ticker") or ""
        try:
            news_items = news_fetcher(tk) if tk else []
        except Exception:
            news_items = []
    ticker = ticker_payload.get("ticker", "")
    classifications = ticker_payload.get("news_classifications")
    if classifications is None:
        classifications = news_classifier.classify_news_items(ticker, news_items or [])
    nws = signals.news_signal(ticker, classifications, direction)
    out_signals["news"] = nws

    # Trump-mention signal. Either pre-computed by the caller (test
    # injection or batched in build_site) and attached to the payload as
    # `trump_signal_result`, or computed on the fly here.
    cfg = gate_config if gate_config is not None else _gate_config()
    trump_finding = None
    if cfg["trump_signal_enabled"]:
        trump_finding = ticker_payload.get("trump_signal_result")
        if trump_finding is None:
            try:
                trump_finding = trump_signal.evaluate(
                    ticker, classifications,
                    ttl_days=cfg["trump_ttl_days"],
                    min_confidence=cfg["trump_min_confidence"],
                )
            except Exception:
                trump_finding = None
    # Stash on the payload so trump_signal_eval can read it without
    # re-running. A disabled signal stays neutral by virtue of an empty
    # finding.
    ticker_payload["trump_signal_result"] = trump_finding or {
        "mention": False, "valence": "none", "confidence": 0.0,
        "as_of": None, "source": "", "summary": "", "manual": False,
        "low_confidence_seen": [],
    }
    trump = signals.trump_signal_eval(ticker_payload, direction)
    out_signals["trump"] = trump

    # ---- Qualification math --------------------------------------------
    # Technical is already a hard prerequisite at this point. The new rule
    # is a "confluence of confirmations" over (sector, news, trump):
    #   confirmations >= trump_confluence_min => qualifies.
    # Default trump_confluence_min=2. When trump.pass is False (the
    # neutral case), this requires BOTH sector AND news -- byte-for-byte
    # identical to the prior 3-of-3 rule. A Trump pass can substitute
    # for one of sector/news.
    sec_pass = bool(sec["pass"])
    news_pass = bool(nws["pass"])
    trump_pass = bool(trump.get("pass"))
    confirmations = sum((sec_pass, news_pass, trump_pass))
    qualifies = confirmations >= cfg["trump_confluence_min"]
    # Optional: technical + trump alone (off by default).
    if (not qualifies and trump_pass and cfg["trump_solo_with_technical"]):
        qualifies = True
    promoted_by_insider = False

    # Insider-promotion path: exactly 1 confirmation (tech + one of
    # sector/news/trump), technical is already verified, insider score
    # >= 2. Same conservative spirit as the old 2-of-3 path -- one full
    # primary still must agree, then an orthogonal Form-4 cluster
    # rescues it.
    if (not qualifies and confirmations == 1 and tech["pass"]
            and allow_insider_promotion):
        insider = _insider_signal(ticker, direction, ticker_payload, insider_fetcher)
        out_signals["insider"] = insider
        if insider.get("score", 0) >= 2 and insider.get("data_available", True):
            qualifies = True
            promoted_by_insider = True

    result = _result(qualifies, out_signals)
    result["promoted_by_insider"] = promoted_by_insider

    # Trump-attack veto on new long entries / endorsement veto on new
    # shorts. We surface the avoid flag regardless of qualifies (so
    # holders see the exit annotation); the veto only flips qualifies
    # when the action is opening/adding to a position.
    if trump.get("avoid") and cfg["trump_attack_vetoes_longs"]:
        is_entry = (action or "").lower() in ("buy", "add", "new_buy",
                                              "sell", "short", "new_short")
        if direction == "long":
            held = ticker_payload.get("position") or {}
            is_held = bool(held)
            if is_entry and not is_held and result["qualifies"]:
                result["qualifies"] = False
                result["trump_block"] = (
                    f"Presidential attack -- {trump.get('reason', '')} "
                    f"[{trump.get('source', '')}]")
            elif is_held:
                result["trump_exit_flag"] = (
                    f"Presidential attack -- {trump.get('reason', '')} "
                    f"[{trump.get('source', '')}]")
        elif direction == "short":
            if is_entry and result["qualifies"]:
                result["qualifies"] = False
                result["trump_block"] = (
                    f"Endorsement vetoes new short -- "
                    f"{trump.get('reason', '')} [{trump.get('source', '')}]")

    # Correlation gate (longs only). Attaches a `correlation` block; for a
    # NEW BUY a high avg correlation to the top-5 holdings downgrades the
    # rec, for an ADD it only annotates (leaning into a known cluster is the
    # user's call).
    if direction == "long" and portfolio is not None and (action or "").lower() in (
            "buy", "add", "new_buy"):
        corr_info, decision, reason = _correlation_assess(
            ticker, action, portfolio, prices_provider)
        result["correlation"] = corr_info
        if decision == "block" and result["qualifies"]:
            result["qualifies"] = False
            result["correlation_block"] = reason
        elif decision == "annotate":
            result["correlation_annotation"] = reason

    # Valuation overlay. Attaches a `valuation` block; for a long buy/add an
    # "extreme" sector valuation downgrades the rec (unless promoted by a
    # score-3 insider cluster), a "cheap" valuation flags a sizing tailwind;
    # for a short a "cheap" valuation just annotates "verify thesis".
    is_long_entry = direction == "long" and (action or "").lower() in (
        "buy", "add", "new_buy")
    if is_long_entry or direction == "short":
        val = _valuation_assess(ticker, fundamentals, sector_comparables)
        if val is not None:
            result["valuation"] = val
            tier = val.get("tier")
            if is_long_entry and tier == "extreme":
                insider = out_signals.get("insider") or {}
                override = promoted_by_insider and insider.get("score") == 3
                if not override and result["qualifies"]:
                    result["qualifies"] = False
                    pct = val.get("percentile_in_sector")
                    result["valuation_block"] = (
                        f"valuation extreme ({pct:.0f}th pct in sector)"
                        if pct is not None else "valuation extreme")
                elif override:
                    result["valuation_override"] = (
                        "extreme valuation overridden by a score-3 insider cluster")
            elif is_long_entry and tier == "cheap":
                result["valuation_tailwind"] = True
            elif direction == "short" and tier == "cheap":
                result["valuation_annotation"] = (
                    "selling at attractive valuation - verify thesis")

    # Earnings-window hard block: only for opening/adding to a long.
    if result["qualifies"] and direction == "long" and (action or "").lower() in (
            "buy", "add", "new_buy"):
        block = _earnings_block(ticker)
        if block:
            result["qualifies"] = False
            result["earnings_block"] = block
    return result


def _valuation_assess(ticker: str, fundamentals: dict | None,
                      sector_comparables: list[dict] | None) -> dict | None:
    """Build the valuation block. Fetches fundamentals + comparables lazily
    when not supplied. Returns None if nothing usable could be computed."""
    from app.research import valuation as val_mod

    if fundamentals is None:
        try:
            from app.data.fundamentals import get_fundamentals
            fundamentals = get_fundamentals(ticker)
        except Exception:
            return None
    if sector_comparables is None:
        sector = (fundamentals or {}).get("sector")
        try:
            sector_comparables = val_mod.build_sector_comparables(sector)
        except Exception:
            sector_comparables = []
    return val_mod.valuation_score(ticker, fundamentals or {}, sector_comparables or [])


def _correlation_assess(ticker: str, action: str | None, portfolio,
                        prices_provider) -> tuple[dict, str, str | None]:
    """Assess a candidate's correlation to the book.

    Returns ``(correlation_dict, decision, reason)`` where decision is one of
    ``block`` (new buy, avg corr to top-5 > 0.7), ``annotate`` (add into a
    tight cluster), or ``ok``.
    """
    from app.research import correlation as corr_mod

    held = {p.ticker.upper() for p in getattr(portfolio, "positions", [])}
    is_add = ticker.upper() in held
    cand = corr_mod.candidate_correlation_to_book(ticker, portfolio, prices_provider)
    info: dict = {"candidate_to_book": cand, "is_add": is_add}

    if not cand.get("available"):
        return info, "ok", None

    if not is_add:
        avg = cand.get("avg_corr_to_top5")
        if avg is not None and avg > 0.7:
            return info, "block", (
                f"high correlation to existing top holdings (avg {avg:.2f})"
            )
        return info, "ok", None

    # ADD: check if the existing position sits in a tight cluster.
    matrix = corr_mod.compute_position_correlations(portfolio, prices_provider)
    row = (matrix.get("matrix") or {}).get(ticker.upper(), {})
    tight = [t for t, c in row.items() if t != ticker.upper() and c > 0.8]
    info["position_matrix_row"] = row
    if len(tight) >= 2:
        return info, "annotate", (
            f"adding into a tight cluster (corr > 0.8 with {', '.join(sorted(tight))})"
        )
    return info, "ok", None


def _earnings_block(ticker: str) -> str | None:
    """Return a block reason if earnings are within 3 trading days, else None."""
    if not ticker:
        return None
    try:
        from app.data.calendar import next_earnings_date
        ed = next_earnings_date(ticker)
    except Exception:
        return None
    if ed is None:
        return None
    days = _trading_days_until(ed)
    if 0 <= days <= 3:
        return f"earnings within 3 trading days ({ed.isoformat()}); wait for the report"
    return None


def _result(qualifies: bool, sigs: dict[str, dict]) -> dict:
    parts = []
    for name in ("technical", "sector_momentum", "news", "trump", "insider"):
        if name in sigs:
            parts.append(f"{name}={'PASS' if sigs[name]['pass'] else 'fail'}")
    # `reasons` is a flat per-signal pass/fail map for telemetry. Signals that
    # were never evaluated (technical short-circuit) read False - they did not
    # pass. `insider_score` is 0 unless the 1-confirmation promotion path ran.
    reasons = {
        name: bool(sigs.get(name, {}).get("pass", False))
        for name in ("technical", "sector_momentum", "news", "trump")
    }
    out = {
        "qualifies": qualifies,
        "signals": sigs,
        "summary": " ".join(parts),
        "reasons": reasons,
        "insider_score": int(sigs.get("insider", {}).get("score", 0) or 0),
    }
    if "insider" in sigs and qualifies:
        out["annotation"] = "promoted on insider cluster"
    return out

