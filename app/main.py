from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import settings, risk_profile
from app.data import news as news_mod
from app.data import prices
from app.portfolio import store
from app.research import analyst, candidates as cands, portfolio_review

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=ROOT / "app" / "templates")

app = FastAPI(title="Portfolio Advisor")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


def _flags() -> dict:
    return {
        "has_anthropic": bool(settings.anthropic_api_key),
        "has_finnhub": bool(settings.finnhub_api_key),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    account = store.load()
    exposures = portfolio_review.compute_exposures(account)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "exposures": exposures,
        "risk": risk_profile(),
        "flags": _flags(),
    })


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations_page(request: Request):
    recs = db.latest_recommendations(limit=50)
    return templates.TemplateResponse("recommendations.html", {
        "request": request,
        "recs": recs,
        "flags": _flags(),
    })


@app.post("/recommendations/run")
def run_recommendations():
    account = store.load()
    exposures = portfolio_review.compute_exposures(account)
    weight_by_ticker = {row["ticker"]: row for row in exposures["positions"]}
    out: list[dict] = []
    for p in account.positions:
        ctx = weight_by_ticker.get(p.ticker, {})
        rec = analyst.analyze_ticker(p.ticker, position_context=ctx)
        db.save_recommendation(rec)
        out.append(rec)
    return {"count": len(out), "recommendations": out}


@app.get("/portfolio/review")
def portfolio_review_api():
    account = store.load()
    return portfolio_review.review(account)


@app.get("/candidates")
def candidates_api():
    account = store.load()
    return cands.candidates(account)


@app.get("/ticker/{ticker}", response_class=HTMLResponse)
def ticker_page(request: Request, ticker: str):
    ticker = ticker.upper()
    q = prices.quote(ticker)
    tech = prices.technicals(ticker)
    news = news_mod.company_news(ticker, days=14)[:15]
    history = db.ticker_history(ticker)
    account = store.load()
    position = account.position(ticker)
    return templates.TemplateResponse("ticker.html", {
        "request": request,
        "ticker": ticker,
        "quote": q.to_dict(),
        "technicals": tech,
        "news": news,
        "history": history,
        "position": position,
        "flags": _flags(),
    })


@app.get("/api/portfolio")
def api_portfolio():
    account = store.load()
    return JSONResponse(portfolio_review.compute_exposures(account))


@app.get("/api/quote/{ticker}")
def api_quote(ticker: str):
    return prices.quote(ticker).to_dict()
