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
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import risk_profile, settings
from app.data import macro as macro_mod
from app.data import market_news, prices
from app.portfolio import store
from app.research import (
    analyst, candidates as cands, daily_brief, llm, metrics as metrics_mod,
    portfolio_review, scanner,
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


def _recent_activity(limit: int = 8) -> list[dict]:
    """Combine trades and notes into a single activity feed, newest first."""
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
        recs.append(rec)
        ticker_payloads[p.ticker] = rec

    print("Generating candidates...")
    try:
        cand_out = cands.candidates(account)
    except Exception as e:
        traceback.print_exc()
        cand_out = {"candidates": [], "error": str(e)}

    print("Writing daily brief...")
    try:
        brief = daily_brief.build(macro, recs, review_out, cand_out, exposures, scan_result, headlines)
    except Exception as e:
        traceback.print_exc()
        brief = {"headline": f"Brief generation failed: {e}",
                 "market_pulse": "", "trade_ideas": [],
                 "portfolio_notes": [], "catalysts_this_week": []}
    print(f"  total LLM attempts so far: {len(llm.ATTEMPTS)}")

    common = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "risk": risk_profile(),
        "flags": {"has_llm": llm.available()},
        "diagnostics": diag,
    }

    env = _env()

    activity = _recent_activity(limit=8)
    _write(site / "index.html", env.get_template("index.html").render(
        brief=brief, macro=macro, exposures=exposures, scan=scan_result,
        recs_by_ticker=ticker_payloads, activity=activity, base="", **common,
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
        candidates=cand_out, base="", **common,
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
        "headlines": headlines,
        "scanner": scan_result,
        "exposures": exposures,
        "review": review_out,
        "metrics": metrics,
        "brief": brief,
        "recommendations": recs,
        "candidates": cand_out,
    }
    (site / "data.json").write_text(json.dumps(data_dump, default=str, indent=2))
    (site / ".nojekyll").write_text("")
    print(f"Built site to {site}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
