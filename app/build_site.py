"""Render the portfolio advisor as a static site at the repo root.

Outputs:
  index.html              morning brief (the page)
  positions.html          full position dashboard
  recommendations.html    per-ticker recommendations
  candidates.html         outside-the-portfolio ideas
  ticker/<SYMBOL>.html    deep dive per ticker
  data.json               full machine-readable dump
  .nojekyll               disables Jekyll
"""
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import risk_profile, settings, sec_user_agent_is_placeholder
from app.data import insider as insider_mod
from app.data import macro as macro_mod
from app.data import market_news, prices
from app import notify
from app.portfolio import idea_queue, rec_history, store
from app.data import fundamentals as fundamentals_mod
from app.research import (
    analyst, candidates as cands, correlation, daily_brief, gate_telemetry,
    idea_funnel, learning, llm, metrics as metrics_mod, portfolio_review,
    regime as regime_mod, scanner, shadow_tracker, valuation,
)


ROOT = Path(__file__).resolve().parent.parent
NOTES_PATH = ROOT / "notes.yaml"
HISTORY_PATH = ROOT / "portfolio_history.yaml"


def _load_yaml_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or []
        return data if isinstance(data, list) else []
    except Exception:
        return []


REC_HISTORY_PATH = ROOT / "rec_history.yaml"
REGIME_HISTORY_PATH = ROOT / "regime_history.json"


def _append_regime_history(regime: dict, breadth: dict) -> None:
    """Append today's regime + breadth to regime_history.json (last 90 days)."""
    history = []
    if REGIME_HISTORY_PATH.exists():
        try:
            history = json.loads(REGIME_HISTORY_PATH.read_text()) or []
        except Exception:
            history = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date": today,
        "regime": regime.get("regime"),
        "confidence": regime.get("confidence"),
        "breadth_pct": (breadth or {}).get("pct_above_sma50"),
    })
    history = history[-90:]
    try:
        REGIME_HISTORY_PATH.write_text(json.dumps(history, indent=0))
    except Exception:
        pass
INTRADAY_ALERTS_PATH = ROOT / "intraday_alerts.json"
INTRADAY_STALE_MIN = 90  # alerts older than this are not rendered


def _load_intraday_alerts() -> dict | None:
    """Return the alerts payload only if it was written within the staleness
    window. Older payloads stay on disk (next intraday run overwrites) but
    don't render on the home page.
    """
    if not INTRADAY_ALERTS_PATH.exists():
        return None
    try:
        data = json.loads(INTRADAY_ALERTS_PATH.read_text())
    except Exception:
        return None
    checked_at = data.get("checked_at") or ""
    try:
        ts = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    if age_min > INTRADAY_STALE_MIN:
        return None
    data["age_minutes"] = round(age_min, 1)
    return data


def _recent_activity(limit: int = 8) -> list[dict]:
    """Combine trades, notes, and resolved recs into a single feed (newest-first).

    Pulls from three sources:
      - ``portfolio_history.yaml`` -- legacy trade log (BUY / SELL / DEPOSIT)
      - ``notes.yaml``             -- free-form thoughts the user typed in chat
      - ``rec_history.yaml``       -- only resolved recs (accepted / rejected /
                                       counter). Pending ones are not surfaced
                                       here; they appear as primary actions.
    """
    activity: list[dict] = []
    for h in _load_yaml_list(HISTORY_PATH):
        activity.append({
            "date": h.get("date") or h.get("logged_at", "")[:10],
            "kind": "trade",
            "summary": h.get("summary", ""),
        })
    for n in _load_yaml_list(NOTES_PATH):
        activity.append({
            "date": n.get("date") or n.get("logged_at", "")[:10],
            "kind": "note",
            "summary": n.get("content", ""),
        })
    for r in _load_yaml_list(REC_HISTORY_PATH):
        status = r.get("status")
        if status not in ("accepted", "rejected", "counter"):
            continue
        date_str = (r.get("resolved_at") or r.get("date") or "")[:10]
        ticker = r.get("ticker") or ""
        action = r.get("action") or ""
        if status == "accepted":
            ep = r.get("executed_price")
            es = r.get("executed_shares")
            tail = f"@ ${ep:.2f} x {es:g} shares" if (ep and es) else ""
            summary = f"Accepted {action.upper()} {ticker} {tail}".strip()
        elif status == "rejected":
            why = (r.get("user_reason") or "").strip()
            summary = f"Rejected {action.upper()} {ticker}" + (f": {why}" if why else "")
        else:  # counter
            cp = r.get("counter_proposal") or {}
            new_act = (cp.get("action") or action).upper()
            summary = f"Counter on {ticker}: {action.upper()} -> {new_act}"
        activity.append({"date": date_str, "kind": "rec", "summary": summary})
    activity.sort(key=lambda r: r.get("date") or "", reverse=True)
    return activity[:limit]


def recompute_total_value(account, exposures: dict,
                          path: Path | None = None) -> tuple[float, bool]:
    """Set ``account.total_value`` from live market values + cash and persist
    when it changed. Returns ``(new_total, changed)``."""
    new_total = round((exposures.get("total_market_value") or 0.0)
                      + (account.cash or 0.0), 2)
    if abs(new_total - (account.total_value or 0.0)) > 0.01:
        account.total_value = new_total
        store.save(account, path=path)
        return new_total, True
    return new_total, False


def _warn_if_placeholder_ua() -> bool:
    """Loudly warn at startup when SEC_USER_AGENT is not a real contact.

    Returns True if a warning was emitted. SEC EDGAR 403s placeholder /
    non-deliverable User-Agents, which makes the insider signal unavailable
    (recorded loudly in telemetry, never a silent zero - see filings.py).
    We do NOT invent an address: the owner must supply a real monitored
    contact via the SEC_USER_AGENT secret/env.
    """
    if not sec_user_agent_is_placeholder():
        return False
    bar = "!" * 64
    print(bar)
    print("WARNING: SEC_USER_AGENT is a placeholder / non-deliverable contact.")
    print(f"  current value: {settings.sec_user_agent!r}")
    print("  SEC EDGAR will 403 this, so the insider-cluster signal will be")
    print("  UNAVAILABLE (data_available=False, surfaced in gate telemetry and")
    print("  the today page - not a silent score-0).")
    print("  TODO(owner): set the SEC_USER_AGENT secret/env to a REAL monitored")
    print("  'Name email@domain' per SEC fair-access policy (see .env.example).")
    print("  Do not use an example.com or @users.noreply.github.com address.")
    print(bar)
    return True


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )


def _write(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _render_page(env: Environment, template_name: str, dest: Path,
                 page_name: str, ctx: dict) -> tuple[str, str] | None:
    """Render one template to ``dest``; never raise.

    On failure print the full traceback and return ``(page_name,
    repr(error))`` so the caller can publish the failure in data.json
    diagnostics instead of aborting the whole refresh.
    """
    try:
        _write(dest, env.get_template(template_name).render(**ctx))
        return None
    except Exception as e:
        traceback.print_exc()
        return (page_name, repr(e))


def render_and_publish(env: Environment, site: Path,
                       pages: list[tuple[str, str, dict]],
                       data_dump: dict) -> list[tuple[str, str]]:
    """Render every page fail-soft, then ALWAYS write data.json + .nojekyll.

    ``pages`` is a list of ``(template_name, output_name, context)``. One
    broken template must not freeze the public site: failures are collected
    per page and published under ``diagnostics.render_errors`` in data.json
    (the canonical artifact) while every healthy page still renders.
    """
    render_errors: list[tuple[str, str]] = []
    for template_name, out_name, ctx in pages:
        err = _render_page(env, template_name, site / out_name, out_name, ctx)
        if err is not None:
            render_errors.append(err)
    # Copy so the template-facing diagnostics dict (string values only)
    # is not aliased with the list we add here.
    diagnostics = dict(data_dump.get("diagnostics") or {})
    diagnostics["render_errors"] = [
        {"page": page, "error": error} for page, error in render_errors
    ]
    data_dump["diagnostics"] = diagnostics
    (site / "data.json").write_text(json.dumps(data_dump, default=str, indent=2))
    (site / ".nojekyll").write_text("")
    if render_errors:
        print("RENDER FAILURES (site still published): "
              + "; ".join(f"{page}: {error}" for page, error in render_errors))
    return render_errors


def main() -> int:
    site = settings.site_dir
    site.mkdir(exist_ok=True)
    (site / "ticker").mkdir(exist_ok=True)

    _warn_if_placeholder_ua()

    print("=== Price source diagnostics ===")
    try:
        diag = prices.diagnose("META")
    except Exception:
        traceback.print_exc()
        diag = {}
    for name, status in diag.items():
        print(f"  {name}: {status}")
    print(f"LLM available: {llm.available()} (synthesis={llm.synthesis_model()}, routine={llm.routine_model()})")
    ping_result = None
    llm.reset_attempts()

    print("Pulling macro snapshot...")
    try:
        macro = macro_mod.snapshot()
    except Exception:
        traceback.print_exc()
        macro = {"indices": {}, "sectors": {}, "leaders": [], "laggards": []}

    print("Detecting market regime...")
    try:
        regime_macro, regime_breadth = regime_mod.gather_regime_inputs()
        regime = regime_mod.detect_regime(regime_macro, regime_breadth)
        print(f"  regime: {regime['regime']} (confidence {regime['confidence']}/3)")
        _append_regime_history(regime, regime_breadth)
    except Exception as e:
        traceback.print_exc()
        regime = {"regime": "chop", "confidence": 1, "factors": {},
                  "summary": f"regime detection failed: {e}"}

    print("Pulling market headlines...")
    try:
        headlines = market_news.top_headlines()
    except Exception:
        traceback.print_exc()
        headlines = []
    print(f"  collected {len(headlines)} headlines from {len({h['source'] for h in headlines})} sources")

    account = store.load()
    held_set = {p.ticker.upper() for p in account.positions}

    print("Running market scanner across universe...")
    try:
        scan_result = scanner.scan(held=held_set)
    except Exception:
        traceback.print_exc()
        scan_result = {
            "universe_size": 0,
            "buckets": {
                "breakouts": [], "momentum_continuation": [], "oversold_bounces": [],
                "pullbacks_to_support": [], "macd_bullish_cross": [], "macd_bearish_cross": [],
                "new_52w_highs": [], "rsi_extreme_overbought": [],
            },
            "top_movers_up": [], "top_movers_down": [],
        }
    print(f"  scanned {scan_result['universe_size']} names; buckets: " +
          ", ".join(f"{k}={len(v)}" for k, v in scan_result["buckets"].items() if v))

    exposures = portfolio_review.compute_exposures(account)
    review_out = portfolio_review.review(exposures)

    # Auto-recompute account.total_value from live position values + cash, so
    # the dashboard's headline number stays current without manual edits.
    try:
        new_total, changed = recompute_total_value(account, exposures)
        if changed:
            print(f"  account.total_value -> ${new_total:,.2f}")
    except Exception:
        traceback.print_exc()

    print("Computing portfolio metrics vs SPY...")
    try:
        metrics = metrics_mod.compute_metrics(account, benchmark="SPY", period="1y")
    except Exception as e:
        traceback.print_exc()
        metrics = {"available": False, "reason": str(e)}
    weight_by_ticker = {row["ticker"]: row for row in exposures["positions"]}

    # Warm the price cache in parallel for everything the position analysis
    # (plus the SPY benchmark and the macro / sector ETF reads) reuses, so the
    # sequential loop below hits the in-memory cache instead of one network
    # round-trip per ticker. Additive only - no change to what is computed.
    prices.prefetch(
        [p.ticker for p in account.positions]
        + ["SPY"]
        + list(macro_mod.INDICES.values())
        + list(macro_mod.SECTOR_ETFS.values())
    )

    print("Analyzing positions...")
    recs: list[dict] = []
    ticker_payloads: dict[str, dict] = {}
    for p in account.positions:
        print(f"  {p.ticker}")
        try:
            rec = analyst.analyze_ticker(p.ticker, position_context=weight_by_ticker.get(p.ticker, {}))
        except Exception as e:
            traceback.print_exc()
            rec = {"ticker": p.ticker, "error": str(e), "action": "hold", "horizon": "long_term",
                   "conviction": 1, "thesis": f"Failed to analyze: {e}",
                   "key_catalysts": [], "key_risks": [], "suggested_action_detail": "",
                   "quote": {}, "technicals": {}, "news": [], "earnings": None,
                   "consensus": None, "analyst_recs": [], "position": {}}
        try:
            rec["correlation_to_book"] = correlation.candidate_correlation_to_book(
                p.ticker, account)
        except Exception:
            traceback.print_exc()
            rec["correlation_to_book"] = {"available": False}
        try:
            f = fundamentals_mod.get_fundamentals(p.ticker)
            comps = valuation.build_sector_comparables(f.get("sector"))
            rec["valuation"] = valuation.valuation_score(p.ticker, f, comps)
        except Exception:
            traceback.print_exc()
            rec["valuation"] = {"tier": "unknown"}
        recs.append(rec)
        ticker_payloads[p.ticker] = rec

    print("Generating candidates...")
    try:
        cand_out = cands.candidates(account)
    except Exception as e:
        traceback.print_exc()
        cand_out = {"candidates": [], "error": str(e)}

    print("Building idea funnel...")
    try:
        insider_on = os.getenv("IDEA_FUNNEL_INSIDER", "1").lower() not in ("0", "false", "")
        funnel = idea_funnel.build(
            scan_result, cand_out.get("screen_results", []), headlines,
            account, insider_scan=insider_on,
        )
        print(f"  {funnel['total_ideas']} ranked ideas "
              f"({len(funnel['confluence'])} multi-signal, "
              f"{funnel['swing_plans']} with swing plans); "
              f"insider clusters found: {funnel['insider_scanned']}")
    except Exception as e:
        traceback.print_exc()
        funnel = {"ideas": [], "total_ideas": 0, "source_counts": {},
                  "insider_scanned": 0, "swing_plans": 0, "confluence": [],
                  "verdicts": {}, "watching_offlist": [], "error": str(e)}

    # Age the idea queue: drop stale `open` entries, recycle 90-day `pass`
    # verdicts back to open. Runs after the funnel's own upsert so today's
    # tickers already have a refreshed last_seen.
    prune_stats = {"dropped_open": 0, "expired_pass": 0}
    try:
        from datetime import date as _date
        funnel_tickers = {(i.get("ticker") or "").upper()
                          for i in funnel.get("ideas", [])}
        queue = idea_queue.load()
        pruned, prune_stats = idea_queue.prune(queue, funnel_tickers, _date.today())
        if prune_stats["dropped_open"] or prune_stats["expired_pass"]:
            idea_queue.save(pruned)
        print(f"  idea queue prune: dropped {prune_stats['dropped_open']} open, "
              f"reset {prune_stats['expired_pass']} pass -> open")
    except Exception:
        traceback.print_exc()

    print("Loading user preferences from rec_history...")
    history = rec_history.load()
    user_prefs = learning.derive_user_preferences(history, lookback_days=30)
    if user_prefs.get("soft_vetoes"):
        print(f"  soft vetoes: {[v['ticker'] for v in user_prefs['soft_vetoes']]}")

    print("Writing daily brief...")
    try:
        brief = daily_brief.build(macro, recs, review_out, cand_out, exposures, scan_result, headlines, account=account, user_preferences=user_prefs, regime=regime, funnel=funnel)
    except Exception as e:
        traceback.print_exc()
        brief = {"headline": f"Brief generation failed: {e}",
                 "market_pulse": "", "trade_ideas": [],
                 "portfolio_notes": [], "catalysts_this_week": []}
    print(f"  total LLM attempts so far: {len(llm.ATTEMPTS)}")

    try:
        added = rec_history.record_pending(brief)
        if added:
            print(f"Logged {len(added)} new pending rec(s) to rec_history.yaml")
    except Exception as e:
        added = []
        traceback.print_exc()

    try:
        payload = notify.write_sidecar(brief, added)
        if payload:
            print(f"Wrote {len(payload)} rec(s) to .new_recs.json for notification")
    except Exception:
        traceback.print_exc()

    if brief.get("telemetry"):
        try:
            gate_telemetry.persist(brief["telemetry"])
            tel = brief["telemetry"]
            print(f"Gate telemetry: {tel['candidates_evaluated']} evaluated, "
                  f"{tel['cleared_primary'] + tel['cleared_insider_promotion']} cleared, "
                  f"{len(tel['near_miss'])} near-miss")
        except Exception:
            traceback.print_exc()

    # Shadow tracker: measurement-only. Wrapped so a price-data outage
    # cannot break the daily refresh.
    rollup = shadow_tracker.safe_update()
    if rollup:
        print(f"Shadow tracker: {rollup.get('total_records', 0)} near-miss records "
              f"across {len(rollup.get('by_failed_signal') or {})} failed-signal groups")
        # Make the Trump section of the rollup available to the
        # today-page template once enough samples exist.
        brief["shadow_calibration"] = {"trump": rollup.get("trump")}

    insider_diag = insider_mod.diagnostics()
    if insider_diag.get("cik_index_error") or insider_diag.get("tickers_unavailable"):
        print(f"Insider data unavailable: "
              f"cik_index={insider_diag.get('cik_index_error')!r}, "
              f"affected_tickers={insider_diag.get('tickers_unavailable')}")

    common = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "risk": risk_profile(),
        "flags": {"has_llm": llm.available()},
        "diagnostics": diag,
        "insider_diagnostics": insider_diag,
        "repo": os.getenv("GITHUB_REPOSITORY", "htbot34/PortfolioManagment"),
    }

    env = _env()

    activity = _recent_activity(limit=8)
    intraday_alerts = _load_intraday_alerts()
    pages: list[tuple[str, str, dict]] = [
        ("index.html", "index.html", dict(
            brief=brief, macro=macro, exposures=exposures, scan=scan_result,
            recs_by_ticker=ticker_payloads, activity=activity, regime=regime,
            intraday=intraday_alerts, base="", **common,
        )),
        ("positions.html", "positions.html", dict(
            exposures=exposures, review=review_out, metrics=metrics, base="",
            **common,
        )),
        ("scanner.html", "scanner.html", dict(scan=scan_result, base="", **common)),
        ("recommendations.html", "recommendations.html", dict(
            recs=recs, base="", **common,
        )),
        ("candidates.html", "candidates.html", dict(
            candidates=cand_out, funnel=funnel, base="", **common,
        )),
    ]
    for ticker, payload in ticker_payloads.items():
        pages.append(("ticker.html", f"ticker/{ticker}.html", dict(
            ticker=ticker, payload=payload, base="../", **common,
        )))

    data_dump = {
        "generated_at": common["generated_at"],
        "diagnostics": diag,
        "insider_diagnostics": insider_diag,
        "llm_ping_result": ping_result,
        "llm_attempts": list(llm.ATTEMPTS),
        "macro": macro,
        "regime": regime,
        "headlines": headlines,
        "scanner": scan_result,
        "exposures": exposures,
        "review": review_out,
        "metrics": metrics,
        "brief": brief,
        "recommendations": recs,
        "candidates": cand_out,
        "idea_funnel": funnel,
        "idea_queue": {"prune_stats": prune_stats},
    }
    # Fail-soft: a single broken page records a render error in data.json
    # instead of aborting the refresh (and freezing the public site).
    # main() still returns 0 so the workflow's Commit step publishes.
    render_and_publish(env, site, pages, data_dump)
    # Persist all of this run's live price fetches in a single write (entries
    # were accumulated in memory rather than re-writing price_cache.json per
    # ticker). An atexit fallback covers partial/early-exit runs.
    prices.flush_persistent_cache()
    print(f"Built site to {site}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
