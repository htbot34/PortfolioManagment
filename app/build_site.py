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

from app.config import risk_profile, settings
from app.data import macro as macro_mod
from app.data import market_news, prices
from app.portfolio import rec_history, store
from app.data import fundamentals as fundamentals_mod
from app.research import (
    analyst, candidates as cands, correlation, daily_brief, idea_funnel,
    learning, llm, metrics as metrics_mod, portfolio_review,
    regime as regime_mod, scanner, valuation,
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


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )


def _write(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main() -> int:
    site = settings.site_dir
    site.mkdir(exist_ok=True)
    (site / "ticker").mkdir(exist_ok=True)

    print("=== Price source diagnostics ===")
    diag = prices.diagnose("META")
    for name, status in diag.items():
        print(f"  {name}: {status}")
    print(f"LLM available: {llm.available()} (synthesis={llm.synthesis_model()}, routine={llm.routine_model()})")
    ping_result = None
    llm.reset_attempts()

    print("Pulling macro snapshot...")
    macro = macro_mod.snapshot()

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
    headlines = market_news.top_headlines()
    print(f"  collected {len(headlines)} headlines from {len({h['source'] for h in headlines})} sources")

    account = store.load()
    held_set = {p.ticker.upper() for p in account.positions}

    print("Running market scanner across universe...")
    scan_result = scanner.scan(held=held_set)
    print(f"  scanned {scan_result['universe_size']} names; buckets: " +
          ", ".join(f"{k}={len(v)}" for k, v in scan_result["buckets"].items() if v))

    exposures = portfolio_review.compute_exposures(account)
    review_out = portfolio_review.review(exposures)

    print("Computing portfolio metrics vs SPY...")
    try:
        metrics = metrics_mod.compute_metrics(account, benchmark="SPY", period="1y")
    except Exception as e:
        traceback.print_exc()
        metrics = {"available": False, "reason": str(e)}
    weight_by_ticker = {row["ticker"]: row for row in exposures["positions"]}

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

    print("Loading user preferences from rec_history...")
    history = rec_history.load()
    user_prefs = learning.derive_user_preferences(history, lookback_days=30)
    if user_prefs.get("soft_vetoes"):
        print(f"  soft vetoes: {[v['ticker'] for v in user_prefs['soft_vetoes']]}")

    print("Writing daily brief...")
    try:
        brief = daily_brief.build(macro, recs, review_out, cand_out, exposures, scan_result, headlines, account=account, user_preferences=user_prefs, regime=regime)
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
        traceback.print_exc()

    common = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "risk": risk_profile(),
        "flags": {"has_llm": llm.available()},
        "diagnostics": diag,
        "repo": os.getenv("GITHUB_REPOSITORY", "htbot34/PortfolioManagment"),
    }

    env = _env()

    activity = _recent_activity(limit=8)
    intraday_alerts = _load_intraday_alerts()
    _write(site / "index.html", env.get_template("index.html").render(
        brief=brief, macro=macro, exposures=exposures, scan=scan_result,
        recs_by_ticker=ticker_payloads, activity=activity, regime=regime,
        intraday=intraday_alerts, base="", **common,
    ))
    _write(site / "positions.html", env.get_template("positions.html").render(
        exposures=exposures, review=review_out, metrics=metrics, base="", **common,
    ))
    _write(site / "scanner.html", env.get_template("scanner.html").render(
        scan=scan_result, base="", **common,
    ))
    _write(site / "recommendations.html", env.get_template("recommendations.html").render(
        recs=recs, base="", **common,
    ))
    _write(site / "candidates.html", env.get_template("candidates.html").render(
        candidates=cand_out, funnel=funnel, base="", **common,
    ))
    tpl_ticker = env.get_template("ticker.html")
    for ticker, payload in ticker_payloads.items():
        _write(site / "ticker" / f"{ticker}.html", tpl_ticker.render(
            ticker=ticker, payload=payload, base="../", **common,
        ))

    data_dump = {
        "generated_at": common["generated_at"],
        "diagnostics": diag,
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
    }
    (site / "data.json").write_text(json.dumps(data_dump, default=str, indent=2))
    (site / ".nojekyll").write_text("")
    print(f"Built site to {site}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
