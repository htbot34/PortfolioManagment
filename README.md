# Portfolio Advisor

**Live dashboard: <https://htbot34.github.io/PortfolioManagment/>**

Static, scheduled stock-portfolio research site. Runs entirely on GitHub. Free.
A GitHub Action refreshes every weekday at 22:00 UTC and after every change to
`portfolio.yaml`.

## How it works

- Reads holdings from `portfolio.yaml` and risk style from `risk_profile.yaml`
- Pulls prices via a fallback chain (Stooq -> Yahoo Chart -> yfinance)
- Pulls recent news from Yahoo + Google News RSS
- Generates buy / hold / trim / sell recommendations via a rule-based engine.
  An LLM (GitHub Models, gpt-4o-mini) is used for semantic news classification
  only. The recommendation engine itself is rule-based by design, so output is
  deterministic and inspectable. LLM failures fall back to keyword scoring.
- Reviews concentration / sector tilt / cash buffer against the risk profile
- Proposes new-position candidates that match the investor's themes
- Runs an **idea funnel**: merges momentum, theme fit, news/social and insider
  cluster buying into one confluence-ranked list, with an ATR-based swing-trade
  plan (entry / stop / target / hold window) on each idea
- Renders to plain HTML at the repo root and serves via GitHub Pages

## Reacting to ideas

Each idea on the candidates page has a **Give a verdict / discuss** link that
opens a prefilled issue. Pick `interested`, `watching`, or `pass`; the verdict
is recorded to `idea_queue.yaml` and feeds back into ranking on the next build
(`pass` drops the idea, `interested` boosts it). Verdicts never change
`portfolio.yaml` -- they are research metadata only.

**Advice only. No order placement. Not financial advice.**

## Why recommendations are rare

The 3-signal conviction gate (technical + sector momentum + news, with an
insider-cluster 2-of-3 promotion path) is intentionally hard to clear. Most
days return `no_trade`. See `gate_telemetry.yaml` for what was evaluated and
why nothing cleared on a given day; the no-trade card on the dashboard
surfaces the closest miss and a 30-day rollup.

## Editing your portfolio

Edit `portfolio.yaml` and push. The workflow auto-refreshes within ~2 minutes.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.build_site
python -m http.server 8000           # browse http://localhost:8000
```
