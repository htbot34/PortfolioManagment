"""Render the portfolio advisor as a static site under docs/.

Entrypoint for the scheduled GitHub Action. Pulls market data, runs the
recommender, writes:
  docs/index.html              dashboard
  docs/recommendations.html    rec feed
  docs/candidates.html         new ideas
  docs/ticker/<SYMBOL>.html    per-ticker deep dive
  docs/data.json               raw payload (for debugging)
"""
import json
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import risk_profile, settings
from app.data import news as news_mod
from app.data import prices
from app.portfolio import store
from app.research import analyst, candidates as cands, llm, portfolio_review


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

    account = store.load()
    exposures = portfolio_review.compute_exposures(account)
    review_out = portfolio_review.review(exposures)
    weight_by_ticker = {row["ticker"]: row for row in exposures["positions"]}

    recs: list[dict] = []
    ticker_payloads: dict[str, dict] = {}
    for p in account.positions:
        try:
            rec = analyst.analyze_ticker(p.ticker, position_context=weight_by_ticker.get(p.ticker, {}))
        except Exception as e:
            traceback.print_exc()
            rec = {"ticker": p.ticker, "error": str(e), "action": "hold", "horizon": "long_term",
                   "conviction": 1, "thesis": f"Failed to analyze: {e}",
                   "key_catalysts": [], "key_risks": [], "suggested_action_detail": "",
                   "quote": {}, "technicals": {}, "news": []}
        recs.append(rec)
        ticker_payloads[p.ticker] = rec

    cand_out = cands.candidates(account)

    common = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "risk": risk_profile(),
        "flags": {"has_llm": llm.available()},
    }

    env = _env()
    _write(site / "index.html", env.get_template("index.html").render(
        exposures=exposures, review=review_out, recs=recs, base="", **common,
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
        "exposures": exposures,
        "review": review_out,
        "recommendations": recs,
        "candidates": cand_out,
    }
    (site / "data.json").write_text(json.dumps(data_dump, default=str, indent=2))
    (site / ".nojekyll").write_text("")
    print(f"Built {len(recs)} recommendations to {site}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
