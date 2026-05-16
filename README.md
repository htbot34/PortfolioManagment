# Portfolio Advisor

**Live dashboard: <https://htbot34.github.io/PortfolioManagment/>**

Static, scheduled stock-portfolio research site. Runs entirely on GitHub. Free.
A GitHub Action refreshes every weekday at 22:00 UTC and after every change to
`portfolio.yaml`.

## How it works

- Reads holdings from `portfolio.yaml` and risk style from `risk_profile.yaml`
- Pulls prices via a fallback chain (Stooq -> Yahoo Chart -> yfinance)
- Pulls recent news from Yahoo + Google News RSS
- Generates buy / hold / trim / sell recommendations via a rule-based engine,
  optionally refined by GitHub Models (free LLM via the workflow token)
- Reviews concentration / sector tilt / cash buffer against the risk profile
- Proposes new-position candidates that match the investor's themes
- Renders to plain HTML at the repo root and serves via GitHub Pages

**Advice only. No order placement. Not financial advice.**

## Editing your portfolio

Edit `portfolio.yaml` and push. The workflow auto-refreshes within ~2 minutes.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.build_site
python -m http.server 8000           # browse http://localhost:8000
```
