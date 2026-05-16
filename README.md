# Portfolio Advisor

LLM-driven swing/long-term research assistant for your stock portfolio.

- Reads your holdings from `portfolio.yaml`
- Pulls live prices and fundamentals (yfinance)
- Pulls recent news (Finnhub)
- Pulls and summarizes SEC filings (EDGAR + Claude Haiku)
- Produces structured buy/hold/trim/sell recommendations per holding (Claude Opus)
- Portfolio-level review (concentration, sector, cash)
- Candidate ideas matching your risk profile
- Dashboard UI at `http://localhost:8000`

**Advice only. No order placement.**

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in ANTHROPIC_API_KEY and FINNHUB_API_KEY in .env

uvicorn app.main:app --reload
```

Open <http://localhost:8000>.

The dashboard works without any API keys — it'll show your positions and live prices via yfinance. News, filings summarization, and recommendations require the corresponding keys.

## Files you edit

- `portfolio.yaml` — your positions (ticker, shares, cost_basis) and cash
- `risk_profile.yaml` — risk tolerance, allowed styles, themes, constraints

## Importing from Webull

Webull → Account → Positions → Export CSV. Then:

```python
from pathlib import Path
from app.portfolio import store, webull_import
acct = store.load()
webull_import.import_csv(Path("webull_positions.csv"), acct)
```

## How recommendations are generated

1. For each holding, yfinance returns the quote, fundamentals, and computed technicals (RSI14, SMA50/200, % off 52w high).
2. Finnhub returns the last 30 days of company news + an overall sentiment score.
3. EDGAR returns the latest 10-K, last two 10-Qs, and recent 8-Ks. Each filing is summarized once by **Claude Haiku** and the summary is cached forever by accession number.
4. **Claude Opus** receives the full bundle plus your `risk_profile.yaml` and emits a JSON recommendation with action, horizon, conviction, thesis, catalysts, and risks. Prompt caching is enabled on the static parts (system prompt, risk profile, filing summaries) so repeated runs are cheap.

The recommendation history is stored in `portfolio.db` (SQLite) so you can see how the model's view of a ticker has evolved over time.

## API endpoints

| Path | Purpose |
|---|---|
| `GET /` | Dashboard |
| `GET /recommendations` | Recommendation feed |
| `POST /recommendations/run` | Run research across all holdings |
| `GET /ticker/{symbol}` | Deep-dive page |
| `GET /portfolio/review` | Concentration / sector review (JSON) |
| `GET /candidates` | New-position candidates (JSON) |
| `GET /api/portfolio` | Exposures as JSON |
| `GET /api/quote/{symbol}` | Single quote |

## Tests

```bash
pytest -q
```
