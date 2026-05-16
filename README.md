# Portfolio Advisor

A static, scheduled stock-portfolio research site. Lives entirely on GitHub. Free.

- Holdings live in `portfolio.yaml`; risk style in `risk_profile.yaml`
- A GitHub Action runs every weekday after market close:
  - Pulls quotes, technicals, and fundamentals (yfinance)
  - Pulls recent news (Yahoo + Google News RSS)
  - Generates buy / hold / trim / sell recommendations via a rule-based engine,
    optionally refined by **GitHub Models** (free LLM via the workflow token)
  - Reviews concentration / sector tilt / cash buffer against your risk profile
  - Proposes new-position candidates that match your themes
  - Renders the result to `docs/` and publishes via GitHub Pages

**Advice only. No order placement. Not financial advice.**

## Live site

Once enabled, the site is at <https://htbot34.github.io/PortfolioManagment/>.

## One-time setup

1. **Enable GitHub Pages** for this repo:
   `Settings` → `Pages` → **Source: GitHub Actions**.
2. **Permit Models access** (already on by default for public repos):
   the workflow declares `permissions: { models: read }`.
3. Optionally edit `portfolio.yaml` and `risk_profile.yaml`.
4. Push to `main` (or hit `Actions` → `Refresh portfolio site` → `Run workflow`).

The workflow commits the regenerated `docs/` back to `main` and publishes it.

## Files you edit

- `portfolio.yaml` — your positions (ticker, shares, cost_basis) and cash
- `risk_profile.yaml` — risk tolerance, allowed styles, themes, constraints
- `.github/workflows/refresh.yml` — schedule (default: weekdays at 22:00 UTC)

## How the recommendation engine works

For each holding:

1. **Market data (free)**: yfinance returns the quote, fundamentals, 1-year
   prices, and computed technicals (RSI(14), SMA50/200, % off 52-week high).
2. **News (free)**: Yahoo Finance + Google News RSS, deduped.
3. **Rule-based signal**: combines trend (SMA stack), momentum (RSI),
   position weight vs concentration cap, drawdown context, and your risk
   profile to produce `{action, horizon, conviction, thesis, catalysts, risks}`.
4. **LLM refinement (free, optional)**: when running inside GitHub Actions
   the workflow's token authenticates GitHub Models (`gpt-4o-mini` by default).
   The model can override the rule action if the news warrants it. If the
   model is unavailable or rate-limited, the rule output stands.

Portfolio review flags concentration > 25%, sector > 45%, cash < 5%, etc., using
the values in `risk_profile.yaml`.

## Running locally (optional)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.build_site            # writes docs/
python -m http.server -d docs 8000  # browse at http://localhost:8000
```

To get LLM commentary locally, create a Personal Access Token with `models:read`
scope and:

```bash
export GITHUB_TOKEN=ghp_yourtoken
python -m app.build_site
```

## Importing from Webull

`Webull` → `Account` → `Positions` → `Export CSV`. Then:

```python
from pathlib import Path
from app.portfolio import store, webull_import
acct = store.load()
webull_import.import_csv(Path("webull_positions.csv"), acct)
```

## Tests

```bash
pytest -q
```
